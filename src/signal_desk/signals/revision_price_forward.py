"""R13b first-observed forward prices for the frozen revision-price research cohort."""

from __future__ import annotations

import datetime as dt
import math

from signal_desk import db, market_clock, store
from signal_desk.broker import execution
from signal_desk.signals import revision_price_freeze as frozen, rotation_shadow

VERSION = "r13-s2-forward-v1"
HORIZON = 20


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def collect(now: dt.datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    session = rotation_shadow.score_completed_session("kr", now)
    if not session or market_clock.is_open("kr", now):
        return {"marked": 0, "reason": "완료된 KR 세션 없음"}
    close = market_clock._calendar("kr").schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=12):
        return {"marked": 0, "reason": "최초 관측 시간창 밖"}
    prices, dates = store.load_portfolio_close_bundle("kr")
    maps = {t: dict(zip(dates[t], ps)) for t, ps in prices.items()
            if t in dates and len(dates[t]) == len(ps)}
    marked = halted = gaps = 0
    for episode in db.revision_price_recent(35):
        origin = episode["session"]
        sessions = market_clock.next_sessions("kr", origin, HORIZON)
        if (episode.get("version") != frozen.VERSION or session <= origin or
                len(sessions) != HORIZON or db.revision_price_halt(origin)):
            continue
        prior = db.revision_price_marks(origin)
        revised = next((f"{t}:{origin}" for t, row in episode["selected"].items()
                        if maps.get(t, {}).get(origin) != row["price"]), None)
        if revised is None:
            revised = next((f"{t}:{day}" for day, panel in prior.items() for t, value in panel.items()
                            if maps.get(t, {}).get(day) != value), None)
        if revised:
            db.revision_price_halt(origin, revised)
            halted += 1
            continue
        if session not in (sessions[0], sessions[-1]):
            continue
        panel = {t: _finite(maps.get(t, {}).get(session)) for t in episode["selected"]}
        if not panel or any(v is None or v <= 0 for v in panel.values()):
            gaps += 1
            continue
        marked += db.revision_price_mark_once(origin, session, panel, int(now.timestamp()))
    return {"marked": marked, "revision_halts": halted, "price_gaps": gaps, "session": session}


def evaluate(snapshot: dict, marks: dict[str, dict[str, float]], *,
             completed_session: str, revision_halt: str | None = None) -> dict:
    shell = {"version": VERSION, "mode": "shadow", "live_eligible": False,
             "source_available_at_verified": False}
    def blocked(reason: str, status: str = "blocked") -> dict:
        return {**shell, "ready": False, "status": status, "reason": reason}
    if snapshot.get("version") != frozen.VERSION or not snapshot.get("fixed_quantities"):
        return blocked("판단 입력/정수주 동결 버전 불일치")
    if revision_halt:
        return blocked("동결 이후 원시 가격 수정/누락: " + revision_halt)
    sessions = market_clock.next_sessions("kr", snapshot["session"], HORIZON)
    if len(sessions) != HORIZON:
        return blocked("거래일 캘린더 범위 밖")
    entry, exit_day = sessions[0], sessions[-1]
    for day in (entry, exit_day):
        if day > completed_session:
            return blocked("전진 가격 대기", "pending")
        panel = marks.get(day)
        if panel is None or any(_finite(panel.get(t)) is None or panel[t] <= 0 for t in snapshot["selected"]):
            return blocked("완료 세션 최초 관측 가격 누락: " + day)
    capital = _finite(snapshot.get("notional"))
    if capital is None or capital <= 0:
        return blocked("가상 원금 오류")
    outcomes = {}
    for policy, tickers in snapshot["policies"].items():
        quantities = snapshot["fixed_quantities"].get(policy) or {}
        if set(quantities) != set(tickers) or any(type(q) is not int or q < 0 for q in quantities.values()):
            return blocked("고정 수량·후보 불일치")
        cash, cost, traded = capital, 0.0, 0.0
        for ticker in tickers:
            qty = quantities[ticker]
            if not qty:
                continue
            fill = execution.calculate(marks[entry][ticker], qty, "buy", "kr",
                                       assumptions=snapshot["cost_assumptions"])
            cash += fill.cash_change
            if cash < -1e-7:
                return blocked("다음 거래일 가격 갭으로 동결 수량 매수 불가")
            cost += fill.total_fees + fill.slippage_cost
            traded += fill.gross_notional
        for ticker in tickers:
            qty = quantities[ticker]
            if not qty:
                continue
            fill = execution.calculate(marks[exit_day][ticker], qty, "sell", "kr",
                                       assumptions=snapshot["cost_assumptions"])
            cash += fill.cash_change
            cost += fill.total_fees + fill.slippage_cost
        outcomes[policy] = {"net_return_pct": round((cash / capital - 1) * 100, 6),
                            "cost_drag_pct": round(cost / capital * 100, 6),
                            "entry_notional_pct": round(traded / capital * 100, 6),
                            "filled_slots": sum(q > 0 for q in quantities.values()),
                            "selected_slots": len(tickers)}
    return {**shell, "ready": True, "status": "complete", "entry_session": entry,
            "exit_session": exit_day, "outcomes": outcomes,
            "delta_net_pp": round(outcomes["revision_unreacted_price"]["net_return_pct"] -
                                  outcomes["eps_revision_only"]["net_return_pct"], 6),
            "note": "최초 관측 뒤 다음 거래일 종가에 가상 진입. 발표 직후 수익/실체결이 아니며 주문 미연결."}
