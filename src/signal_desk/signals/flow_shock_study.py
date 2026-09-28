"""R16 KR 수급 충격의 최초 관측·동일 업종 가격 대조 전진 연구. 주문 미연결."""

from __future__ import annotations

import datetime as dt
import math

from signal_desk import db, market_clock, store
from signal_desk.broker import execution
from signal_desk.reference import sectors

VERSION = "r16-flow-shock-v1"
START_SESSION = "2026-09-29"
HORIZON = 5
MIN_DROP_PCT = -3.0
SHOCK_INTENSITY = -0.15
CONTROL_INTENSITY = -0.05
MAX_PRICE_MATCH_GAP_PP = 2.0


def _number(value) -> float | None:
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n if math.isfinite(n) else None


def _window(now: dt.datetime) -> tuple[str | None, dt.datetime | None, str | None]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    session = market_clock.latest_completed_session("kr", now)
    if not session or market_clock.is_open("kr", now):
        return None, None, "KR 완료 세션 없음"
    close = market_clock._calendar("kr").schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=12):
        return session, close, "최초 관측 시간창 밖"
    return session, close, None


def _flag(value) -> bool:
    return value is True or value == 1


def _inputs(session: str, close: dt.datetime, now: dt.datetime) -> tuple[list[dict], dict]:
    flows = store.load_flow_first_observations(session, after=close, as_of=now)
    history = store.load_signal_history("kr")
    if flows.empty or history.empty or "date" not in history.columns:
        return [], {"reason": "동일 완료 세션 수급 또는 PIT 시그널 없음"}
    pit = history[history["date"].astype(str) == session]
    if pit.empty or not {"ticker", "bar_asof", "session_valid", "observed_at"} <= set(pit.columns):
        return [], {"reason": "PIT 세션 메타데이터 없음"}
    if pit["ticker"].duplicated().any():
        return [], {"reason": "PIT 종목 중복"}
    signals = {str(r["ticker"]): r for r in pit.to_dict("records")}
    prices, dates = store.load_portfolio_close_bundle("kr")
    previous = market_clock.previous_session("kr", session)
    eligible = []
    excluded = {"flow_invalid": 0, "signal_missing_or_stale": 0, "price_missing_or_stale": 0,
                "sector_missing": 0}
    for row in flows.to_dict("records"):
        ticker = str(row.get("ticker") or "")
        try:
            flow_seen = dt.datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
            flow_fresh = (flow_seen.tzinfo is not None and
                          close <= flow_seen.astimezone(dt.timezone.utc) <= now)
        except (TypeError, ValueError, KeyError):
            flow_fresh = False
        if str(row.get("date")) != session or not str(row.get("content_hash") or "") or not flow_fresh:
            excluded["flow_invalid"] += 1
            continue
        flow = [_number(row.get(key)) for key in ("foreign_net", "inst_net", "volume")]
        if any(v is None for v in flow) or flow[2] <= 0:
            excluded["flow_invalid"] += 1
            continue
        intensity = (flow[0] + flow[1]) / flow[2]
        if abs(intensity) > 1:
            excluded["flow_invalid"] += 1
            continue
        signal = signals.get(ticker)
        if not signal or signal.get("market") not in (None, "kr") or \
                signal.get("session_valid") is not True or \
                str(signal.get("bar_asof")) != session or signal.get("exchange_session") != session:
            excluded["signal_missing_or_stale"] += 1
            continue
        try:
            seen = dt.datetime.fromisoformat(str(signal["observed_at"]).replace("Z", "+00:00"))
            if seen.tzinfo is None or not close <= seen.astimezone(dt.timezone.utc) <= now:
                excluded["signal_missing_or_stale"] += 1
                continue
        except (TypeError, ValueError):
            excluded["signal_missing_or_stale"] += 1
            continue
        ds, ps = dates.get(ticker) or [], prices.get(ticker) or []
        if len(ds) != len(ps) or len(ds) < 2 or ds[-2:] != [previous, session]:
            excluded["price_missing_or_stale"] += 1
            continue
        before, current = _number(ps[-2]), _number(ps[-1])
        if before is None or before <= 0 or current is None or current <= 0:
            excluded["price_missing_or_stale"] += 1
            continue
        sector = sectors.sector_of(ticker)
        if not sector:
            excluded["sector_missing"] += 1
            continue
        adverse = "veto_observed" if (_flag(signal.get("event_risk")) or
                                      _flag(signal.get("decision_blocked")) or
                                      signal.get("decision_severity") in ("serious", "critical")) \
                  else "no_veto_recorded"
        eligible.append({"ticker": ticker, "sector": sector, "price": current,
                         "previous_price": before, "return_pct": round((current / before - 1) * 100, 6),
                         "sell_intensity": round(intensity, 6), "adverse_context": adverse,
                         "flow_hash": str(row["content_hash"]),
                         "flow_observed_at": str(row["observed_at"])})
    return eligible, {"flow_rows": len(flows), "pit_rows": len(pit), "eligible": len(eligible),
                      "excluded": excluded}


def capture(now: dt.datetime) -> dict:
    session, close, problem = _window(now)
    if problem or not session or not close or session < START_SESSION:
        return {"saved": 0, "session": session, "reason": problem or "연구 시작 전 세션"}
    eligible, quality = _inputs(session, close, now)
    if not eligible:
        return {"saved": 0, "session": session, **quality}
    assumptions = execution.cost_assumptions("kr")
    saved = unmatched = 0
    for row in eligible:
        if row["return_pct"] > MIN_DROP_PCT or row["sell_intensity"] > SHOCK_INTENSITY:
            continue
        controls = [r for r in eligible if r["ticker"] != row["ticker"]
                    and r["sector"] == row["sector"] and r["adverse_context"] == row["adverse_context"]
                    and r["return_pct"] <= MIN_DROP_PCT and r["sell_intensity"] > CONTROL_INTENSITY
                    and abs(r["return_pct"] - row["return_pct"]) <= MAX_PRICE_MATCH_GAP_PP]
        control = min(controls, key=lambda r: (abs(r["return_pct"] - row["return_pct"]), r["ticker"])) \
                  if controls else None
        unmatched += int(control is None)
        snapshot = {"version": VERSION, "session": session, "ticker": row["ticker"],
                    "captured_at": now.isoformat(), "mode": "research_only", "live_eligible": False,
                    "source_available_at_verified": False,
                    "hypothesis": "매도 수급 충격 후 다음 KR 거래일 종가 진입의 5세션 비용 후 상대경로",
                    "candidate": row, "control": control, "cost_assumptions": assumptions,
                    "parameters": {"drop_pct": MIN_DROP_PCT, "shock_intensity": SHOCK_INTENSITY,
                                   "control_intensity": CONTROL_INTENSITY,
                                   "max_price_match_gap_pp": MAX_PRICE_MATCH_GAP_PP,
                                   "horizon_sessions": HORIZON}}
        saved += int(db.flow_shock_add_once(session, row["ticker"], snapshot))
    return {"saved": saved, "session": session, "unmatched_candidates": unmatched, **quality}


def collect(now: dt.datetime) -> dict:
    session, _, problem = _window(now)
    if problem or not session:
        return {"marked": 0, "halted": 0, "session": session, "reason": problem}
    snapshots = db.flow_shock_snapshots(None)
    if not snapshots:
        return {"marked": 0, "halted": 0, "session": session}
    prices, dates = store.load_portfolio_close_bundle("kr")
    maps = {ticker: dict(zip(dates[ticker], closes)) for ticker, closes in prices.items()
            if ticker in dates and len(dates[ticker]) == len(closes)}
    marked = halted = gaps = 0
    for snap in snapshots:
        origin, ticker = snap["session"], snap["ticker"]
        if snap.get("version") != VERSION or db.flow_shock_halt(origin, ticker):
            continue
        future = market_clock.next_sessions("kr", origin, HORIZON)
        if len(future) != HORIZON:
            db.flow_shock_halt(origin, ticker, "전진 공식 거래 세션 부족")
            halted += 1
            continue
        due = (future[0], future[-1])
        prior = db.flow_shock_marks(origin, ticker)
        selected = [snap["candidate"]] + ([snap["control"]] if snap.get("control") else [])
        revisions = [(r["ticker"], origin) for r in selected
                     if _number(maps.get(r["ticker"], {}).get(origin)) != r["price"]]
        revisions += [(t, day) for day, mark in prior.items() for t, price in mark["prices"].items()
                      if _number(maps.get(t, {}).get(day)) != price]
        if revisions:
            db.flow_shock_halt(origin, ticker, "동결 후 원시 가격 수정/누락")
            halted += 1
            continue
        if any(day < session and day not in prior for day in due):
            db.flow_shock_halt(origin, ticker, "전진 가격 최초 관측 세션 누락")
            halted += 1
            continue
        if session not in due or session in prior:
            continue
        panel = {r["ticker"]: _number(maps.get(r["ticker"], {}).get(session)) for r in selected}
        if any(price is None or price <= 0 for price in panel.values()):
            gaps += 1
            continue
        marked += int(db.flow_shock_mark_once(origin, ticker, session, panel, int(now.timestamp())))
    return {"marked": marked, "halted": halted, "gaps": gaps, "session": session}


def _net_return(entry: float, end: float, assumptions: dict) -> float:
    buy = execution.calculate(entry, 1, "buy", "kr", assumptions=assumptions)
    sell = execution.calculate(end, 1, "sell", "kr", assumptions=assumptions)
    return (sell.cash_change / -buy.cash_change - 1) * 100


def evaluate(snapshot: dict, marks: dict[str, dict], halt: str | None = None) -> dict:
    shell = {"mode": "research_only", "live_eligible": False}
    if halt:
        return {**shell, "status": "halted", "reason": halt}
    if not snapshot.get("control"):
        return {**shell, "status": "unmatched", "reason": "동일 업종·낙폭·악재 플래그 대조 종목 없음"}
    future = market_clock.next_sessions("kr", snapshot["session"], HORIZON)
    if len(future) != HORIZON or any(day not in marks for day in (future[0], future[-1])):
        return {**shell, "status": "pending"}
    entry, end = marks[future[0]]["prices"], marks[future[-1]]["prices"]
    candidate, control = snapshot["candidate"]["ticker"], snapshot["control"]["ticker"]
    if any(t not in entry or t not in end or _number(entry[t]) is None or _number(end[t]) is None
           or entry[t] <= 0 or end[t] <= 0 for t in (candidate, control)):
        return {**shell, "status": "blocked", "reason": "전진 가격 무결성 부족"}
    assumptions = snapshot["cost_assumptions"]
    c = _net_return(entry[candidate], end[candidate], assumptions)
    baseline = _net_return(entry[control], end[control], assumptions)
    return {**shell, "status": "matured", "candidate_net_pct": round(c, 5),
            "control_net_pct": round(baseline, 5), "paired_delta_pp": round(c - baseline, 5),
            "entry_session": future[0], "exit_session": future[-1]}


def report() -> dict:
    snapshots = db.flow_shock_snapshots(None)
    counts = {"pending": 0, "unmatched": 0, "halted": 0, "blocked": 0, "matured": 0}
    groups: dict[str, list[float]] = {"veto_observed": [], "no_veto_recorded": []}
    recent = []
    for snap in snapshots:
        day, ticker = snap["session"], snap["ticker"]
        outcome = evaluate(snap, db.flow_shock_marks(day, ticker), db.flow_shock_halt(day, ticker))
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
        if outcome["status"] == "matured":
            groups[snap["candidate"]["adverse_context"]].append(outcome["paired_delta_pp"])
        if len(recent) < 20:
            recent.append({"session": day, "ticker": ticker, "adverse_context":
                           snap["candidate"]["adverse_context"], "status": outcome["status"],
                           "paired_delta_pp": outcome.get("paired_delta_pp")})
    return {"version": VERSION, "mode": "forward_research", "live_eligible": False,
            "source_available_at_verified": False, "observed": len(snapshots), "counts": counts,
            "cohorts": {key: {"n": len(values), "mean_delta_pp":
                               round(sum(values) / len(values), 4) if values else None}
                        for key, values in groups.items()}, "recent": recent,
            "note": "악재 플래그 미관측은 악재 부재 증명이 아니다. 네이버 원천 공개시각·호가·기업행동 "
                    "미검증, 수급 충격 기준/대조군은 연구용 고정 가정. 충분한 독립 관측과 비용·위험 검증 "
                    "전에는 주문·시그널 승격 금지."}
