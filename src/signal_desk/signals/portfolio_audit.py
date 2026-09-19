"""Content-addressed shadow decisions. Recorded inputs are not proof of PIT availability.

The calendar describes scheduled cash sessions, not broker tradability or a live quote.
Never fabricate publication timestamps for legacy price/signal caches.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from signal_desk.broker import execution
from signal_desk.jsonutil import json_safe
from signal_desk.signals import portfolio_decision

SCHEMA_VERSION = 1
HORIZON = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@lru_cache(maxsize=1)
def engine_version() -> str:
    base = Path(__file__).parent
    modules = [base / name for name in ("portfolio_decision.py", "portfolio_candidates.py",
               "portfolio_construction.py", "portfolio_risk.py", "portfolio_trade_plan.py", "portfolio_audit.py")]
    modules.append(base.parent / "broker" / "execution.py")
    return hashlib.sha256(b"".join(p.read_bytes() for p in modules) + np.__version__.encode()).hexdigest()


def clock_context(market: str, now: datetime | None = None) -> dict:
    """Freeze next-session-close evaluation dates; never choose an already open session."""
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("timezone-aware decision time required")
    name = {"kr": "XKRX", "us": "XNYS"}[market]
    base = {"calendar": name, "calendar_version": xcals.__version__,
            "quote_verified": False, "source_available_at_verified": False,
            "bar_finality_verified": False, "live_eligible": False}
    try:
        calendar = xcals.get_calendar(name)
        schedule = calendar.schedule
        before = schedule[schedule["close"] < pd.Timestamp(now)]
        after = schedule[schedule["open"] > pd.Timestamp(now)].iloc[:HORIZON + 1]
        if before.empty or len(after) != HORIZON + 1:
            raise ValueError("calendar horizon unavailable")
        # No weekday fallback outside the supplied calendar's coverage.
        if pd.Timestamp(now).date() < calendar.first_session.date() or pd.Timestamp(now).date() > calendar.last_session.date():
            raise ValueError("calendar date outside coverage")
        return {**base, "ready": True, "expected_price_session": before.index[-1].date().isoformat(),
                "evaluation_sessions": [{"date": day.date().isoformat(), "open": row["open"].isoformat(),
                                          "close": row["close"].isoformat()} for day, row in after.iterrows()],
                "entry_convention": "next_unopened_session_close"}
    except (ValueError, KeyError, IndexError):
        return {**base, "ready": False, "reason": "거래일 캘린더 범위/일정 확인 불가"}


def capture(*, rows: list[dict], universe: list[dict], signal_by_ticker: dict, prices: dict,
            dates_by: dict, profile: dict, market: str) -> tuple[dict, dict]:
    """Freeze every input used by the joint decision, including rejected candidates.

    Only fields consumed by the policy are stored for signals; this replays the portfolio
    decision, not the upstream signal generation. Quotes are deliberately not imported.
    """
    tickers = {str(r["ticker"]) for r in rows + universe}
    policy_profile = {k: v for k, v in profile.items() if k not in ("updated", "configured")}
    inputs = json_safe({"rows": rows, "universe": universe, "profile": policy_profile, "market": market,
                       "prices": {t: prices.get(t, []) for t in sorted(tickers)},
                       "dates_by": {t: dates_by.get(t, []) for t in sorted(tickers)},
                       "signals": {t: {k: getattr(s, k, None) for k in ("kind", "score", "event_risk")}
                                   for t, s in signal_by_ticker.items() if t in tickers},
                       "assumptions": execution.cost_assumptions(market)})
    # Round-trip detaches mutable caches and guarantees replay uses exactly the saved values.
    inputs = json.loads(canonical(inputs))
    result = _run(inputs)
    timing = clock_context(market)
    selected = {r["ticker"] for r in rows}
    selected.update(i["ticker"] for i in result["entry_candidates"].get("candidates", []))
    # Even a rejected BUY candidate can affect ranking/correlation selection of others.
    selected.update(t for t, s in inputs["signals"].items()
                    if s.get("kind") in ("BUY", "STRONG_BUY") and not s.get("event_risk"))
    invalid = []
    for ticker in sorted(selected):
        days, values = inputs["dates_by"][ticker], inputs["prices"][ticker]
        try:
            normalized = [datetime.strptime(d, "%Y-%m-%d").date().isoformat() for d in days]
            valid = bool(days) and days == normalized and days == sorted(set(days)) and len(days) == len(values)
            valid = valid and days[-1] == timing.get("expected_price_session")
            valid = valid and all(isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0 for p in values)
            if valid:
                calendar = xcals.get_calendar(timing["calendar"])
                valid = all(calendar.is_session(d) for d in days)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            invalid.append(ticker)
    timing["aligned"] = bool(timing["ready"] and selected and not invalid)
    timing["invalid_tickers"] = invalid
    timing["reason"] = ("최종 완료 거래일·가격 이력 정합성 확인" if timing["aligned"] else
                        "거래일/최종 가격 시점 미확인 — 성과 측정 및 실행 보류")
    body = {"schema_version": SCHEMA_VERSION, "engine_version": engine_version(), "inputs": inputs,
            "result": result, "timing": timing,
            "limitations": ["수집 원본의 공개 시각·확정봉 인증 전", "상위 시그널 생성 과정은 재현 범위 밖",
                            "수정주가·기업행동 원장 미확인", "실거래 승인 아님"]}
    return result, body


def _run(inputs: dict) -> dict:
    args = {k: v for k, v in inputs.items() if k != "signals"}
    args["signal_by_ticker"] = {t: SimpleNamespace(**s) for t, s in inputs["signals"].items()}
    try:
        return portfolio_decision.decide(**args)
    except (ValueError, TypeError, OverflowError):
        return portfolio_decision._blocked("입력 수치/날짜 검증 실패 — 통합 판단 보류")


def replay(body: dict) -> dict:
    compatible = body.get("schema_version") == SCHEMA_VERSION and body.get("engine_version") == engine_version()
    if not compatible:
        return {"ready": False, "matched": None, "reason": "저장 당시와 엔진 버전이 달라 재계산을 보류합니다."}
    computed = _run(body["inputs"])
    matched = digest(computed) == digest(body["result"])
    return {"ready": True, "matched": matched, "reason": "동결 입력·비용으로 결과 일치" if matched else "재계산 결과 불일치"}


def summary(artifact_id: str, body: dict, created: int) -> dict:
    timing = body["timing"]
    return {"id": artifact_id, "recorded_at": created, "policy_version": body["result"]["decision"]["policy_version"],
            "engine_version": body["engine_version"], "timing": timing, "limitations": body["limitations"],
            "integrity_verified": True, "live_eligible": False,
            "plan_ready": bool(body["result"]["trade_plan"].get("ready"))}
