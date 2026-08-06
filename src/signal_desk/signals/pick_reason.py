"""픽 이유 — 시그널이 '왜 골랐/막혔는지'를 구조화해 사후 재생한다.

북극성 A(선택 품질)의 설명 축. 점수·kind만 남기면 판별력이 있어도 학습이 안 된다.
봇 저널·PIT 스냅샷이 같은 스키마를 쓰도록 `from_signal` 한곳을 공유한다.
"""

from __future__ import annotations

import json
from typing import Any

# PIT·저널에 넣을 reasons 상한 — parquet/JSON 비대화 방지. 화면은 앞쪽이 중요.
_MAX_REASONS = 24


def from_signal(s: Any) -> dict:
    """SignalResult → 사후 재생용 dict (결정론, LLM 없음)."""
    dec = getattr(s, "decision", None)
    factors = dict(getattr(s, "factor_scores", None) or {})
    if not factors:
        # factor_scores가 비어 있어도 원시 팩터로 최소한의 분해를 남긴다.
        for k, attr in (
            ("technical", "technical_score"),
            ("fundamental", "fundamental_score"),
            ("reversion", "reversion_score"),
            ("flow", "flow_intensity"),
            ("quality", "quality_points"),
            ("momentum", "momentum_ret"),
            ("short", "short_ratio"),
            ("qualitative", "qualitative_score"),
        ):
            v = getattr(s, attr, None)
            if v is not None:
                factors[k] = v
        vp = getattr(s, "valuation_percentile", None)
        if vp is not None:
            factors["valuation"] = vp
    reasons = list(getattr(s, "reasons", None) or [])[:_MAX_REASONS]
    return {
        "ticker": getattr(s, "ticker", ""),
        "name": getattr(s, "name", "") or getattr(s, "ticker", ""),
        "score": getattr(s, "score", None),
        "kind": getattr(s, "kind", None),
        "confidence": getattr(s, "confidence", None),
        "rank": getattr(s, "rank", None),
        "rank_pct": getattr(s, "rank_pct", None),
        "rank_eligible": bool(getattr(s, "rank_eligible", False)),
        "gate_blocked": bool(getattr(s, "gate_blocked", False)),
        "event_risk": bool(getattr(s, "event_risk", False)),
        "factors": factors,
        "reasons": reasons,
        "decision": dec.to_dict() if dec is not None and hasattr(dec, "to_dict") else None,
    }


def history_meta(s: Any) -> dict:
    """PIT 행에 붙일 스칼라·JSON 필드 (snapshot_signals용)."""
    pr = from_signal(s)
    dec = pr.get("decision") or {}
    return {
        "rank": pr["rank"],
        "rank_eligible": int(bool(pr["rank_eligible"])),
        "gate_blocked": int(bool(pr["gate_blocked"])),
        "event_risk": int(bool(pr["event_risk"])),
        "decision_severity": (dec.get("severity") or "") or None,
        "decision_blocked": int(bool(dec.get("buy_blocked"))),
        "decision_summary": (dec.get("summary") or "")[:200] or None,
        "reasons_json": json.dumps(pr["reasons"], ensure_ascii=False),
    }


def parse_reasons_json(raw: Any) -> list[str]:
    if raw is None or (isinstance(raw, float) and raw != raw):  # NaN
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw][:_MAX_REASONS]
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            v = json.loads(raw)
            return [str(x) for x in v][:_MAX_REASONS] if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def from_history_row(row: dict) -> dict:
    """signal_history 한 행 → pick-reason 스키마 (구행은 메타 결손 가능)."""
    factors = {}
    for k in ("technical", "fundamental", "valuation", "reversion", "qualitative",
              "flow", "quality", "momentum", "short"):
        if k in row and row[k] is not None and row[k] == row[k]:
            factors[k] = row[k]
    return {
        "ticker": str(row.get("ticker") or ""),
        "name": str(row.get("ticker") or ""),
        "date": str(row.get("date") or ""),
        "score": row.get("score"),
        "kind": row.get("kind"),
        "confidence": None,
        "rank": row.get("rank"),
        "rank_pct": None,
        "rank_eligible": bool(row.get("rank_eligible")),
        "gate_blocked": bool(row.get("gate_blocked")),
        "event_risk": bool(row.get("event_risk")),
        "factors": factors,
        "reasons": parse_reasons_json(row.get("reasons_json")),
        "decision": {
            "buy_blocked": bool(row.get("decision_blocked")),
            "severity": row.get("decision_severity"),
            "summary": row.get("decision_summary") or "",
            "holding_action": None,
            "event_id": None,
            "policy_version": None,
        } if (row.get("decision_blocked") is not None
              or row.get("decision_severity")
              or row.get("decision_summary")) else None,
    }


def postmortem(date: str, ticker: str, *, history_rows: list[dict],
               closes_by_ticker: dict, bot_decisions: list[dict] | None = None,
               horizons: tuple[int, ...] = (5, 20)) -> dict:
    """날짜·종목 기준 사후 분석 — PIT ⊕ 실현수익 ⊕ (있으면) 봇 판단."""
    from signal_desk.signals import accuracy as acc

    rows = [r for r in history_rows
            if str(r.get("date")) == date and str(r.get("ticker")) == ticker]
    if not rows:
        return {"ready": False, "blocked_reason": f"{date} {ticker} PIT 스냅샷 없음"}
    pick = from_history_row(rows[0])
    dates_closes = closes_by_ticker.get(ticker)
    fwd: dict[str, float | None] = {}
    if dates_closes:
        rets = acc.forward_returns(dates_closes[0], dates_closes[1], date, horizons)
        fwd = {f"h{h}": round(rets[h] * 100, 2) if h in rets else None for h in horizons}
    else:
        fwd = {f"h{h}": None for h in horizons}
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo("Asia/Seoul")
    except Exception:  # pragma: no cover
        _tz = _dt.timezone(_dt.timedelta(hours=9))
    decisions = []
    for d in bot_decisions or []:
        if str(d.get("ticker")) != ticker:
            continue
        ts = d.get("ts")
        if ts:
            day = _dt.datetime.fromtimestamp(int(ts), _tz).strftime("%Y-%m-%d")
            if day != date:
                continue
        decisions.append({
            "action": d.get("action"),
            "score": d.get("score"),
            "rationale": d.get("rationale"),
            "outcome_pct": d.get("outcome_pct"),
            "pick": (d.get("context") or {}).get("pick"),
            "ts": d.get("ts"),
        })
    return {
        "ready": True,
        "date": date,
        "ticker": ticker,
        "pick": pick,
        "forward_ret_pct": fwd,
        "bot_decisions": decisions[:10],
        "note": "forward는 시그널 다음 거래일 진입·horizon 종가(accuracy 규약).",
    }


def latest(ticker: str, *, history_rows: list[dict],
           closes_by_ticker: dict,
           bot_decisions: list[dict] | None = None) -> dict:
    """종목의 가장 최근 PIT 날짜로 postmortem. 없으면 ready=False."""
    dates = sorted(
        {str(r.get("date")) for r in history_rows if str(r.get("ticker")) == ticker
         and r.get("date")},
        reverse=True,
    )
    if not dates:
        return {"ready": False, "blocked_reason": f"{ticker} PIT 스냅샷 없음"}
    return postmortem(
        dates[0], ticker,
        history_rows=history_rows,
        closes_by_ticker=closes_by_ticker,
        bot_decisions=bot_decisions,
    )


def slim_for_detail(pm: dict) -> dict | None:
    """시그널 상세 히어로용 — 있을 때만 한 줄. 새 패널을 만들지 않는다."""
    if not pm or not pm.get("ready"):
        return None
    pick = pm.get("pick") or {}
    fwd = pm.get("forward_ret_pct") or {}
    return {
        "date": pm.get("date"),
        "kind": pick.get("kind"),
        "score": pick.get("score"),
        "rank": pick.get("rank"),
        "gate_blocked": bool(pick.get("gate_blocked")),
        "forward_ret_pct": {"h5": fwd.get("h5"), "h20": fwd.get("h20")},
        "reasons": list(pick.get("reasons") or [])[:4],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 픽 이유 재생 — 목록 (2026-08-06)
#
# 북극성 A의 절반은 "고른 이유를 사후 재생"인데, 상세(`postmortem`)만 있고 **무엇을 골랐는지
# 고르는 화면이 없었다** — date·ticker를 손으로 알아야 쓸 수 있어서 사실상 닿을 수 없었다.
#
# 새 라우트를 만들지 않고 같은 라우트가 `ticker` 없이 오면 목록을 낸다. 고아 라우트 허용목록이
# 10/10 만석이라, 진입점을 늘리는 것이 곧 그 상한을 미는 것이다.
# ─────────────────────────────────────────────────────────────────────────────

# 스냅샷에 근거·순위 컬럼이 추가된 날. 이 앞 스냅샷은 `reasons_json`·`rank`가 비어 있다 —
# **결함이 아니라 스키마 이전**이고, 화면이 공백으로 두면 "이유가 없는 픽"으로 읽힌다.
# 실측(2026-08-06): `reasons_json` non-null 400/2400행 = 12일 중 2일뿐.
SCHEMA_REASONS_FROM = "2026-08-04"


def _nn(v):
    """NaN·inf → None. pandas 결손이 그대로 JSON 에 실리는 것을 막는다."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if f != f or f in (float("inf"), float("-inf")) else v


def available_dates(history_rows: list[dict]) -> list[dict]:
    """스냅샷 날짜 목록 — 최신 우선. 각 날짜가 **근거를 기록했는지**까지 낸다.

    근거 유무를 날짜마다 내는 이유: 비어 있는 것이 수집 실패인지 스키마 이전인지
    화면이 구분해서 말해야 한다(0의 이유 규칙).
    """
    by_date: dict[str, dict] = {}
    for r in history_rows:
        d = str(r.get("date") or "")
        if not d:
            continue
        agg = by_date.setdefault(d, {"date": d, "rows": 0, "with_reasons": 0, "buys": 0})
        agg["rows"] += 1
        if parse_reasons_json(r.get("reasons_json")):
            agg["with_reasons"] += 1
        if str(r.get("kind") or "") in ("BUY", "STRONG_BUY"):
            agg["buys"] += 1
    out = []
    for d in sorted(by_date, reverse=True):
        a = by_date[d]
        a["reasons_recorded"] = a["with_reasons"] > 0
        # 없는 이유를 붙인다 — 화면이 문구를 조립하면 규약이 갈라진다.
        a["reasons_note"] = None if a["reasons_recorded"] else (
            f"근거·순위 컬럼 추가({SCHEMA_REASONS_FROM}) 이전 스냅샷 — 수집 실패가 아니다")
        out.append(a)
    return out


def picks_on(date: str, *, history_rows: list[dict], closes_by_ticker: dict,
             names: dict[str, str] | None = None, limit: int = 40,
             horizons: tuple[int, ...] = (5, 20)) -> dict:
    """그 날짜의 픽 목록 — 점수 내림차순. 실현수익을 붙여 **사후** 재생이 되게 한다.

    `postmortem`과 같은 `forward_returns` 규약을 쓴다(다음 거래일 진입 → h거래일 종가).
    두 곳에서 조립하면 목록과 상세의 수익률이 갈라지고 그 차이는 어디에도 안 뜬다.
    """
    from signal_desk.signals import accuracy as acc

    rows = [r for r in history_rows if str(r.get("date")) == date]
    if not rows:
        return {"ready": False, "blocked_reason": f"{date} PIT 스냅샷 없음",
                "date": date, "picks": [], "dates": available_dates(history_rows)}
    nm = names or {}
    picks = []
    for r in rows:
        t = str(r.get("ticker") or "")
        sc = _nn(r.get("score"))
        dc = closes_by_ticker.get(t)
        fwd: dict[str, float | None] = {f"h{h}": None for h in horizons}
        if dc:
            rets = acc.forward_returns(dc[0], dc[1], date, horizons)
            for h in horizons:
                if h in rets:
                    fwd[f"h{h}"] = round(rets[h] * 100, 2)
        picks.append({
            "ticker": t, "name": nm.get(t) or t,
            "score": sc, "kind": r.get("kind"),
            "rank": r.get("rank") if r.get("rank") == r.get("rank") else None,
            "rank_eligible": bool(r.get("rank_eligible")),
            "gate_blocked": bool(r.get("gate_blocked")),
            # NaN 은 유효 JSON 이 아니다 — FastAPI 가 정화해 주지만 CLI·테스트가 직접
            # json.dumps 하면 터진다. 산출물 쪽에서 None 으로 통일한다.
            "data_coverage": _nn(r.get("data_coverage")),
            "n_reasons": len(parse_reasons_json(r.get("reasons_json"))),
            "forward_ret_pct": fwd,
        })
    picks.sort(key=lambda p: (p["score"] is None, -(p["score"] or 0)))
    meta = next((d for d in available_dates(history_rows) if d["date"] == date), None)
    return {
        "ready": True, "date": date,
        "picks": picks[:limit], "total": len(picks),
        "shown": min(limit, len(picks)),
        # 잘렸으면 밝힌다 — 조용한 truncation 은 "전부 봤다"로 읽힌다.
        "truncated": len(picks) > limit,
        "reasons_recorded": bool(meta and meta["reasons_recorded"]),
        "reasons_note": (meta or {}).get("reasons_note"),
        "dates": available_dates(history_rows),
        "note": "forward는 시그널 다음 거래일 진입·horizon 종가(accuracy 규약).",
    }
