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
