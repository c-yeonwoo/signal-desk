"""실보유 포트폴리오의 데이터 품질·집중도·행동 우선순위 진단.

이 모듈은 예측 수익률을 만들어내거나 주문을 내지 않는다. 가격·섹터·상관 데이터가 확인된
범위에서만 구조적 위험을 계산하고, 확인되지 않은 값은 '알 수 없음'으로 보존한다.
"""

from __future__ import annotations

from collections import defaultdict


def _pct(value: float, total: float) -> float:
    return round(value / total * 100, 1) if total > 0 else 0.0


def analyze(*, rows: list[dict], cash: float, profile: dict, risk: dict, market: str,
            currency: str, as_of: str) -> dict:
    """검증된 가격 행과 제외된 행을 받아 포트폴리오 진단을 만든다.

    rows 원소: ticker/name/value/price/history_ready/sector. 가격 부재 종목은 value=None으로
    전달돼 데이터 품질의 결손으로만 계산하며 임의 평가액으로 채우지 않는다.
    """
    valued = [r for r in rows if r.get("value") is not None and float(r["value"]) > 0]
    missing_price = [r["ticker"] for r in rows if r.get("value") is None]
    missing_history = [r["ticker"] for r in valued if not r.get("history_ready")]
    missing_sector = [r["ticker"] for r in valued if not r.get("sector")]
    invested = sum(float(r["value"]) for r in valued)
    cash = max(0.0, float(cash or 0.0))
    total = invested + cash
    # portfolio_risk는 주식 보유분만을 분모로 쓴다. 사용자가 설정한 한도는 현금을 포함한
    # 전체 자산 기준이므로 여기서 분모를 맞춘다.
    risk = {**risk, "clusters": [{**cluster,
            "weight_pct": _pct(float(cluster.get("weight_pct") or 0) * invested / 100, total)}
            for cluster in (risk.get("clusters") or [])]}
    holdings = []
    sector_values: dict[str, float] = defaultdict(float)
    for row in valued:
        item = {**row, "weight_pct": _pct(float(row["value"]), total)}
        holdings.append(item)
        sector_values[row.get("sector") or "미분류"] += float(row["value"])
    holdings.sort(key=lambda x: x["value"], reverse=True)
    sectors = [{"sector": sector, "value": round(value, 2), "weight_pct": _pct(value, total)}
               for sector, value in sorted(sector_values.items(), key=lambda x: x[1], reverse=True)]

    coverage = {
        "positions_input": len(rows), "priced_positions": len(valued),
        "price_coverage_pct": _pct(len(valued), len(rows)) if rows else 0.0,
        "history_ready_positions": len(valued) - len(missing_history),
        "history_coverage_pct": _pct(len(valued) - len(missing_history), len(valued)) if valued else 0.0,
        "sector_coverage_pct": _pct(len(valued) - len(missing_sector), len(valued)) if valued else 0.0,
        "missing_price_tickers": missing_price, "missing_history_tickers": missing_history,
        "missing_sector_tickers": missing_sector,
    }
    price_ok = bool(rows) and not missing_price
    # 추정 대신 등급 하향. 상관·섹터 결손은 가격이 정확하더라도 위험 진단 신뢰도를 낮춘다.
    if price_ok and not missing_history and not missing_sector:
        quality, confidence = "complete", "high"
    elif price_ok:
        quality, confidence = "partial", "medium"
    else:
        quality, confidence = "insufficient", "low"

    limits = {key: float(profile[key]) for key in (
        "max_single_position_pct", "max_sector_pct", "max_cluster_pct", "min_cash_pct")}
    guidance = []
    if not rows:
        guidance.append({"priority": "blocker", "kind": "input", "action": "보유종목 입력",
                         "reason": "포트폴리오 분석에는 종목·수량·평단 입력이 필요합니다.", "confidence": "high"})
    if missing_price:
        guidance.append({"priority": "blocker", "kind": "data_quality", "action": "시세 데이터 보완",
                         "reason": f"{len(missing_price)}개 보유종목의 현재 시세가 없어 총액·비중을 확정할 수 없습니다.",
                         "tickers": missing_price, "confidence": "high"})
    if missing_history:
        guidance.append({"priority": "high", "kind": "data_quality", "action": "위험 추정 보류",
                         "reason": f"{len(missing_history)}개 종목은 상관·변동성에 필요한 과거 시세가 부족합니다.",
                         "tickers": missing_history, "confidence": "high"})
    for item in holdings:
        if item["weight_pct"] > limits["max_single_position_pct"]:
            guidance.append({"priority": "high", "kind": "concentration", "action": "단일 종목 비중 검토",
                             "ticker": item["ticker"], "name": item.get("name"), "current_pct": item["weight_pct"],
                             "limit_pct": limits["max_single_position_pct"],
                             "reason": f"현재 {item['weight_pct']}%로 설정 한도 {limits['max_single_position_pct']:.1f}%를 초과합니다.",
                             "confidence": "high"})
    for item in sectors:
        if item["sector"] != "미분류" and item["weight_pct"] > limits["max_sector_pct"]:
            guidance.append({"priority": "high", "kind": "sector", "action": "섹터 집중 완화 검토",
                             "sector": item["sector"], "current_pct": item["weight_pct"],
                             "limit_pct": limits["max_sector_pct"],
                             "reason": f"{item['sector']} 비중이 {item['weight_pct']}%로 설정 한도를 넘습니다.",
                             "confidence": "high"})
    for cluster in risk.get("clusters") or []:
        if cluster["weight_pct"] > limits["max_cluster_pct"] and len(cluster["tickers"]) > 1:
            guidance.append({"priority": "high", "kind": "correlation", "action": "고상관 묶음 축소 검토",
                             "tickers": cluster["tickers"], "current_pct": cluster["weight_pct"],
                             "limit_pct": limits["max_cluster_pct"],
                             "reason": f"서로 고상관인 종목 묶음이 {cluster['weight_pct']}%입니다.",
                             "confidence": "medium"})
    cash_pct = _pct(cash, total)
    if total > 0 and cash_pct < limits["min_cash_pct"]:
        guidance.append({"priority": "medium", "kind": "liquidity", "action": "현금 완충 검토",
                         "current_pct": cash_pct, "limit_pct": limits["min_cash_pct"],
                         "reason": f"현금 비중 {cash_pct}%가 설정 최소치 {limits['min_cash_pct']:.1f}%보다 낮습니다.",
                         "confidence": "high"})
    if not guidance and total > 0:
        guidance.append({"priority": "normal", "kind": "monitor", "action": "현재 비중 유지·정기 재검토",
                         "reason": "입력한 집중도·현금 한도 위반은 발견되지 않았습니다. 이는 수익 예측이나 매수 권고가 아닙니다.",
                         "confidence": confidence})
    priority_order = {"blocker": 0, "high": 1, "medium": 2, "normal": 3}
    guidance.sort(key=lambda x: priority_order[x["priority"]])
    return {
        "ready": bool(rows), "market": market, "currency": currency, "as_of": as_of,
        "data_quality": {"status": quality, "confidence": confidence, **coverage},
        "profile": profile, "summary": {"total_value": round(total, 2), "invested_value": round(invested, 2),
                    "cash": round(cash, 2), "cash_pct": cash_pct, "positions": len(valued)},
        "holdings": holdings, "sectors": sectors, "risk": risk, "guidance": guidance,
        "disclaimer": "구조적 위험·제약 점검 결과입니다. 수익 예측이나 자동 주문이 아니며, 데이터 결손은 추정하지 않습니다.",
    }
