"""R12 price-only control vs sector-shrunk 12-1 momentum. Research only.

One non-overlapping 20-session episode per market; first-seen closes and decisions
are immutable. Neither ranking nor outcome is wired to orders or live signals.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import asdict

from signal_desk import db, market_clock, store
from signal_desk.broker import execution
from signal_desk.reference import sectors
from signal_desk.signals import engine, rotation_shadow

VERSION = "r12-s1-price-v1"
HORIZON = 20
TOP_PCT = 3.0
MIN_SECTOR = 5
SHRINK_N = 10
NOTIONAL = {"kr": 100_000_000.0, "us": 100_000.0}  # illustrative, not account capital


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def rank_candidates(rows: list[dict], sector_of: dict[str, str | None], k: int) -> dict:
    """A common PIT price universe; no sector z-scores on tiny peer groups."""
    market_median = statistics.median(r["momentum"] for r in rows)
    groups: dict[str, list[float]] = {}
    for row in rows:
        sector = sector_of.get(row["ticker"])
        if sector:
            groups.setdefault(sector, []).append(row["momentum"])
    ranked = []
    for row in rows:
        group = groups.get(sector_of.get(row["ticker"]) or "", [])
        shrink = len(group) / (len(group) + SHRINK_N) if len(group) >= MIN_SECTOR else 0.0
        benchmark = market_median + shrink * (statistics.median(group) - market_median) if shrink else market_median
        ranked.append({**row, "sector": sector_of.get(row["ticker"]), "peer_count": len(group),
                       "sector_shrink": round(shrink, 6),
                       "relative_momentum": round(row["momentum"] - benchmark, 8)})
    baseline = sorted(ranked, key=lambda r: (-r["baseline_score"], r["ticker"]))[:k]
    challenger = sorted((r for r in ranked if r["momentum"] > 0),
                        key=lambda r: (-r["relative_momentum"], r["ticker"]))[:k]
    return {"price_3factor": [r["ticker"] for r in baseline],
            "sector_momentum": [r["ticker"] for r in challenger],
            "details": {r["ticker"]: {key: r[key] for key in ("baseline_score", "momentum", "sector",
                                                             "peer_count", "sector_shrink", "relative_momentum")}
                        for r in baseline + challenger}}


def _sector_map(market: str) -> dict[str, str | None]:
    if market == "kr":
        return dict(sectors.SECTOR_OF)
    return {str(r["ticker"]): r.get("sector") for r in store.load_us_universe()}


def capture(market: str, now: dt.datetime) -> dict:
    if market not in NOTIONAL or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market and timezone-aware now required")
    session = market_clock.latest_completed_session(market, now)
    if not session or market_clock.is_open(market, now):
        return {"saved": 0, "reason": "완료 세션 없음"}
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=20 if market == "us" else 12):
        return {"saved": 0, "reason": "최초 관측 시간창 밖"}
    if market == "us" and db.kv_get("us_signal_snapshot_session") != session:
        return {"saved": 0, "reason": "미국 PIT 스냅샷 세션 불일치"}
    recent = db.price_baseline_recent(market, 1)
    if recent:
        origin = recent[0]["session"]
        if session <= origin:
            return {"saved": 0, "reason": "이미 동결됨"}
        forward = market_clock.next_sessions(market, origin, HORIZON)
        if len(forward) != HORIZON or session <= forward[-1]:
            return {"saved": 0, "reason": "20거래일 비중첩 창 진행 중"}
    history = store.load_signal_history(market)
    if history.empty or "date" not in history.columns:
        return {"saved": 0, "reason": "PIT 시그널 없음"}
    pit = history[history["date"].astype(str) == session]
    if pit.empty or any(c not in pit.columns for c in ("bar_asof", "session_valid", "momentum")):
        return {"saved": 0, "reason": "PIT 메타데이터 없음"}
    if pit["ticker"].duplicated().any():
        return {"saved": 0, "reason": "PIT 중복 종목"}
    prices, dates = store.load_portfolio_close_bundle(market)
    cfg = engine.SignalConfig()
    calendar = market_clock._calendar(market)
    earliest = (dt.date.fromisoformat(session) - dt.timedelta(days=550)).isoformat()
    expected = [day.date().isoformat() for day in calendar.sessions_in_range(earliest, session)][-(cfg.momentum_lookback + 1):]
    if len(expected) != cfg.momentum_lookback + 1:
        return {"saved": 0, "reason": "252거래일 공식 캘린더 이력 부족"}
    candidates = []
    for item in pit.to_dict("records"):
        ticker = str(item["ticker"])
        if item.get("session_valid") is not True or str(item.get("bar_asof")) != session:
            continue
        closes, days = prices.get(ticker), dates.get(ticker)
        if not closes or not days or len(closes) != len(days) or days[-1] != session:
            continue
        if len(closes) <= cfg.momentum_lookback or days[-len(expected):] != expected or any(
                _finite(p) is None or p <= 0 for p in closes):
            continue
        ret = closes[-1 - cfg.momentum_skip] / closes[-1 - cfg.momentum_lookback] - 1
        pit_ret = _finite(item.get("momentum"))
        if pit_ret is None or abs(pit_ret - ret) > 0.00011:
            continue  # PIT value and raw price generation disagree; do not reconstruct later.
        series = engine.compute_indicator_series(closes, cfg)
        score = _finite(engine.combine(engine._price_only_components(closes, series, len(closes) - 1, cfg), cfg)["score"])
        price = _finite(closes[-1])
        if score is None or price is None or price <= 0:
            continue
        candidates.append({"ticker": ticker, "price": price, "baseline_score": score,
                           "momentum": ret})
    coverage = len(candidates) / len(pit)
    if len(candidates) < 50 or coverage < 0.95:
        return {"saved": 0, "reason": "PIT/원시가격·252일 이력 정합성 미달",
                "eligible": len(candidates), "pit_rows": len(pit), "coverage": round(coverage, 4)}
    k = engine.rank_slots(len(candidates), TOP_PCT)
    ranked = rank_candidates(candidates, _sector_map(market), k)
    selected = set(ranked["price_3factor"] + ranked["sector_momentum"])
    snapshot = {"version": VERSION, "market": market, "session": session, "observed_at": now.isoformat(),
                "mode": "shadow", "live_eligible": False, "pit_rows": len(pit), "eligible": len(candidates),
                "coverage": round(coverage, 6), "top_k": k, "horizon_sessions": HORIZON,
                "notional": NOTIONAL[market], "cost_assumptions": execution.cost_assumptions(market),
                "config": asdict(cfg), "sector_min_n": MIN_SECTOR, "sector_shrink_n": SHRINK_N,
                "policies": {p: ranked[p] for p in ("price_3factor", "sector_momentum")},
                "selected": {t: {"price": next(r["price"] for r in candidates if r["ticker"] == t),
                                 **ranked["details"][t]} for t in selected},
                "limits": "동일 PIT 종가 연구; 비중첩 단일 에피소드, 정수주·비용 포함. 배당/기업행동/호가/실제 체결/가격 최종확정 미포함."}
    return {"saved": int(db.price_baseline_add_once(market, session, snapshot)), "session": session,
            "eligible": len(candidates), "top_k": k}


def collect_forward(market: str, now: dt.datetime) -> dict:
    if market not in NOTIONAL or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market and timezone-aware now required")
    session = rotation_shadow.score_completed_session(market, now)
    if not session or market_clock.is_open(market, now):
        return {"marked": 0, "reason": "완료 세션 없음"}
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=20 if market == "us" else 12):
        return {"marked": 0, "reason": "최초 관측 시간창 밖"}
    prices, dates = store.load_portfolio_close_bundle(market)
    maps = {t: dict(zip(dates[t], ps)) for t, ps in prices.items()
            if t in dates and len(dates[t]) == len(ps)}
    marked = halted = gaps = 0
    for episode in db.price_baseline_recent(market, 2):
        origin = episode["session"]
        sessions = market_clock.next_sessions(market, origin, HORIZON)
        if session <= origin or len(sessions) != HORIZON or db.price_baseline_halt(market, origin):
            continue
        previous = db.price_baseline_marks(market, origin)
        revised = next((f"{t}:{origin}" for t, row in episode["selected"].items()
                        if maps.get(t, {}).get(origin) != row["price"]), None)
        if revised is None:
            revised = next((f"{t}:{day}" for day, panel in previous.items() for t, price in panel.items()
                            if maps.get(t, {}).get(day) != price), None)
        if revised:
            db.price_baseline_halt(market, origin, revised)
            halted += 1
            continue
        if session not in (sessions[0], sessions[-1]):
            continue
        panel = {t: _finite(maps.get(t, {}).get(session)) for t in episode["selected"]}
        if not panel or any(p is None or p <= 0 for p in panel.values()):
            gaps += 1
            continue
        marked += db.price_baseline_mark_once(market, origin, session, panel, int(now.timestamp()))
    return {"marked": marked, "revision_halts": halted, "price_gaps": gaps, "session": session}


def evaluate(snapshot: dict, marks: dict[str, dict[str, float]], *,
             completed_session: str, revision_halt: str | None = None) -> dict:
    base = {"version": VERSION, "mode": "shadow", "live_eligible": False}
    def blocked(reason: str, status: str = "blocked") -> dict:
        return {**base, "ready": False, "status": status, "reason": reason}
    if snapshot.get("version") != VERSION or revision_halt:
        return blocked("버전 불일치 또는 최초 관측 가격 수정: " + str(revision_halt or ""))
    sessions = market_clock.next_sessions(snapshot["market"], snapshot["session"], HORIZON)
    if len(sessions) != HORIZON:
        return blocked("거래일 캘린더 범위 밖")
    entry, exit_day = sessions[0], sessions[-1]
    for day in (entry, exit_day):
        if day > completed_session:
            return blocked("전진 가격 대기", "pending")
        panel = marks.get(day)
        if panel is None or any(_finite(panel.get(t)) is None or panel[t] <= 0 for t in snapshot["selected"]):
            return blocked("완료 세션 최초 관측 가격 누락: " + day)
    outcome = {}
    capital = snapshot["notional"]
    market = snapshot["market"]
    assumptions = snapshot["cost_assumptions"]
    for policy, tickers in snapshot["policies"].items():
        cash, holdings, cost, entry_notional = capital, {}, 0.0, 0.0
        slot = capital / max(1, snapshot["top_k"])
        for ticker in tickers:
            frozen_unit = -execution.calculate(snapshot["selected"][ticker]["price"], 1, "buy", market,
                                               assumptions=assumptions).cash_change
            qty = int(slot // frozen_unit)
            if qty < 1:
                continue  # identical fixed per-slot budget; unfilled slots stay cash.
            fill = execution.calculate(marks[entry][ticker], qty, "buy", market, assumptions=assumptions)
            cash += fill.cash_change
            if cash < -1e-7:
                return blocked("다음 거래일 가격 갭으로 동결 수량 매수 불가")
            holdings[ticker] = qty
            cost += fill.total_fees + fill.slippage_cost
            entry_notional += fill.gross_notional
        for ticker, qty in holdings.items():
            fill = execution.calculate(marks[exit_day][ticker], qty, "sell", market, assumptions=assumptions)
            cash += fill.cash_change
            cost += fill.total_fees + fill.slippage_cost
        outcome[policy] = {"net_return_pct": round((cash / capital - 1) * 100, 6),
                           "cost_drag_pct": round(cost / capital * 100, 6),
                           "entry_notional_pct": round(entry_notional / capital * 100, 6),
                           "filled_slots": len(holdings), "selected_slots": len(tickers)}
    return {**base, "ready": True, "status": "complete", "entry_session": entry,
            "exit_session": exit_day, "outcomes": outcome,
            "delta_net_pp": round(outcome["sector_momentum"]["net_return_pct"] -
                                  outcome["price_3factor"]["net_return_pct"], 6),
            "note": "한 번의 비중첩 종가 가상매수·매도. 실제 성과/전략 승격 증거가 아님."}
