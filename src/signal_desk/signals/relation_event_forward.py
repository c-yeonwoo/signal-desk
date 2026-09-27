"""First-observed, costed forward marks for R15 event cohorts; research only."""

from __future__ import annotations

import datetime as dt
import math

from signal_desk import db, market_clock, store
from signal_desk.broker import execution
from signal_desk.signals import relation_event_study as study

VERSION = "r15-us-kr-forward-v1"


def _valid(value) -> float | None:
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def collect(now: dt.datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    session = market_clock.latest_completed_session("kr", now)
    if not session or market_clock.is_open("kr", now):
        return {"marked": 0, "reason": "KR 완료 세션 없음"}
    close = market_clock._calendar("kr").schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=12):
        return {"marked": 0, "reason": "최초 관측 시간창 밖"}
    snapshots = db.relation_event_snapshots()
    if not snapshots:
        return {"marked": 0, "halted": 0, "gaps": 0, "session": session}
    prices, dates = store.load_portfolio_close_bundle("kr")
    maps = {t: dict(zip(dates[t], ps)) for t, ps in prices.items()
            if t in dates and len(dates[t]) == len(ps)}
    marked = halted = gaps = 0
    for snapshot in snapshots:
        eid, origin = snapshot["event_id"], snapshot["session"]
        if snapshot.get("version") != study.COHORT_VERSION or db.relation_event_halt(eid):
            continue
        sessions = market_clock.next_sessions("kr", origin, study.HORIZON)
        if len(sessions) != study.HORIZON:
            db.relation_event_halt(eid, "20개 공식 전진 거래 세션 확보 실패")
            halted += 1
            continue
        entry, exit_day = sessions[0], sessions[-1]
        if session <= origin:
            continue
        prior = db.relation_event_marks(eid)
        if session > exit_day:
            missing = next((due for due in (entry, exit_day) if due not in prior), None)
            if missing:
                db.relation_event_halt(eid, "전진 가격 최초 관측 세션 누락: " + missing)
                halted += 1
            continue
        selected = snapshot["selected"]
        revised = next((f"{t}:{origin}" for t, row in selected.items()
                        if _valid(maps.get(t, {}).get(origin)) != row["price"]), None)
        if revised is None:
            revised = next((f"{t}:{day}" for day, mark in prior.items()
                            for t, price in mark["prices"].items()
                            if _valid(maps.get(t, {}).get(day)) != price), None)
        if revised:
            db.relation_event_halt(eid, "동결 후 원시 가격 수정/누락: " + revised)
            halted += 1
            continue
        for due in (entry, exit_day):
            if due < session and due not in prior:
                db.relation_event_halt(eid, "전진 가격 최초 관측 세션 누락: " + due)
                halted += 1
                break
        else:
            if session not in (entry, exit_day) or session in prior:
                continue
            panel = {t: _valid(maps.get(t, {}).get(session)) for t in selected}
            if not panel or any(p is None for p in panel.values()):
                gaps += 1
                continue
            marked += int(db.relation_event_mark_once(eid, session, panel, int(now.timestamp())))
    return {"marked": marked, "halted": halted, "gaps": gaps, "session": session}


def evaluate(snapshot: dict | None, marks: dict[str, dict], *, completed_session: str | None,
             halt: str | None = None) -> dict:
    shell = {"version": VERSION, "mode": "research_only", "live_eligible": False,
             "source_available_at_verified": False}

    def blocked(reason: str, status: str = "blocked") -> dict:
        return {**shell, "ready": False, "status": status, "reason": reason}

    if halt:
        return blocked(halt)
    if not snapshot or snapshot.get("version") != study.COHORT_VERSION:
        return blocked("첫 국내 완료 세션의 비교 모집단 미동결")
    sessions = market_clock.next_sessions("kr", snapshot["session"], study.HORIZON)
    if len(sessions) != study.HORIZON:
        return blocked("20개 공식 전진 거래 세션 확보 실패")
    entry, exit_day = sessions[0], sessions[-1]
    if not completed_session or exit_day > completed_session:
        return blocked("20거래일 전진 가격 대기", "pending")
    for day in (entry, exit_day):
        panel = marks.get(day, {}).get("prices")
        if not panel or set(panel) != set(snapshot["selected"]) or any(
                _valid(v) is None for v in panel.values()):
            return blocked("최초 관측 전진 가격 누락: " + day)
    capital = snapshot.get("notional")
    if not isinstance(capital, (int, float)) or not math.isfinite(capital) or capital <= 0:
        return blocked("가상 원금 오류")
    outcomes = {}
    for policy in ("linked", "sector_control"):
        quantities = snapshot.get("quantities", {}).get(policy) or {}
        if not quantities or any(type(q) is not int or q <= 0 for q in quantities.values()):
            return blocked("고정 정수주 수량 오류")
        cash, cost, entry_notional = float(capital), 0.0, 0.0
        for ticker, qty in quantities.items():
            if ticker not in marks[entry]["prices"]:
                return blocked("진입 종목 가격 누락")
            fill = execution.calculate(marks[entry]["prices"][ticker], qty, "buy", "kr",
                                       assumptions=snapshot["cost_assumptions"])
            cash += fill.cash_change
            cost += fill.total_fees + fill.slippage_cost
            entry_notional += fill.gross_notional
        if cash < -1e-7:
            return blocked("다음 세션 종가 갭으로 고정 수량 매수 불가")
        for ticker, qty in quantities.items():
            fill = execution.calculate(marks[exit_day]["prices"][ticker], qty, "sell", "kr",
                                       assumptions=snapshot["cost_assumptions"])
            cash += fill.cash_change
            cost += fill.total_fees + fill.slippage_cost
        outcomes[policy] = {"net_return_pct": round((cash / capital - 1) * 100, 6),
                            "cost_drag_pct": round(cost / capital * 100, 6),
                            "entry_notional_pct": round(entry_notional / capital * 100, 6)}
    gap = abs(outcomes["linked"]["entry_notional_pct"] -
              outcomes["sector_control"]["entry_notional_pct"])
    if gap > study.MAX_EXPOSURE_GAP_PP:
        return blocked("실제 가상 진입 원금 노출 차이 1%p 초과")
    delta = outcomes["linked"]["net_return_pct"] - outcomes["sector_control"]["net_return_pct"]
    direction = snapshot.get("direction")
    if direction not in (1, -1):
        return blocked("사건 방향 오류")
    return {**shell, "ready": True, "status": "complete", "entry_session": entry,
            "exit_session": exit_day, "outcomes": outcomes, "raw_delta_net_pp": round(delta, 6),
            "directional_delta_net_pp": round(direction * delta, 6),
            "entry_observed_at": marks[entry]["observed_at"],
            "exit_observed_at": marks[exit_day]["observed_at"],
            "note": "양쪽 모두 가상 매수/매도. 하향 사건의 부호 반전은 상대성과 연구이며 공매도 수익이 아닙니다."}
