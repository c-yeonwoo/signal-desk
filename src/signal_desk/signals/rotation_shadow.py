"""R11 사전등록 저회전 *로테이션* shadow. 주문/챔피언 정책에는 연결하지 않는다.

같은 마감 보유와 PIT 시그널을 두 정책에 공급한다. 이는 전체 봇 백테스트가 아니라
로테이션 의사결정의 paired 관측이다. 위험 청산·예약·신규 편입·분할매수는 범위 밖이다.
"""

from __future__ import annotations

import datetime as dt
import json
import math

from signal_desk import db, market_clock, store, strategy
from signal_desk.bot import REFERENCE_BOTS
from signal_desk.broker import execution
from signal_desk.signals import engine

VERSION = "rotation-shadow-s0-v2"
# 사후 최적화 금지. 상위 3% 신규매수, 상위 10% 계속보유, 주 1회, 최소 5 거래일.
CHALLENGER = {"entry_top_pct": 3.0, "hold_top_pct": 10.0,
              "min_hold_sessions": 5, "review": "first_observed_session_each_iso_week",
              "loss_exclusion": False}


def _finite(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fixed_orders(snapshot: dict, pairs: list[dict]) -> list[dict] | None:
    """판단 시점 종가와 비용 가정에서 정수주를 확정. 미래 가격으로 수량 재최적화 금지."""
    market = snapshot["market"]
    assumptions = snapshot["cost_assumptions"]
    cash = float(snapshot["cash"])
    orders = []
    for pair in pairs:
        sale = execution.calculate(pair["out_close"], pair["out_qty"], "sell", market,
                                   assumptions=assumptions)
        cash += sale.cash_change
        unit_buy = -execution.calculate(pair["in_close"], 1, "buy", market,
                                        assumptions=assumptions).cash_change
        qty = int(min(cash, snapshot["tranche_alloc"]) // unit_buy) if unit_buy > 0 else 0
        if qty < 1:
            return None
        buy = execution.calculate(pair["in_close"], qty, "buy", market,
                                  assumptions=assumptions)
        cash += buy.cash_change
        if cash < -1e-7:
            return None
        orders += [{"ticker": pair["out"], "side": "sell", "qty": pair["out_qty"]},
                   {"ticker": pair["in"], "side": "buy", "qty": qty}]
    return orders


def decide(snapshot: dict, style: str, *, review_due: bool) -> dict:
    """동일 입력의 회전 결정만 비교. 점수를 기대수익률로 변환하지 않는다."""
    rp = strategy.rotation_params(style)
    held = {h["ticker"]: h for h in snapshot["holdings"]}
    sigs = {s["ticker"]: s for s in snapshot["signals"]}
    n = int(snapshot.get("signal_count") or len(sigs))
    warned = set(snapshot["warned"])
    cooled = set(snapshot["recent_sold"])
    candidates = sorted(
        (s for s in sigs.values() if s["ticker"] not in held
         and engine.is_buy(s["kind"]) and not s["event_risk"]
         and s["ticker"] not in warned and s["ticker"] not in cooled
         and s["score"] >= snapshot["min_buy_score"]),
        key=lambda s: (-s["score"], s["ticker"]))
    results = {}
    for policy in ("champion_rotation_proxy", "s0_rank_buffer"):
        slots = max(0, int(snapshot["max_positions"]) - len(held))
        if slots and not (rp["when_slots_free"] and snapshot["cash"] < snapshot["tranche_alloc"]):
            results[policy] = {"action": "hold", "reason": "기존 보유 슬롯 여유", "pairs": []}
            continue
        if policy == "s0_rank_buffer" and not review_due:
            results[policy] = {"action": "hold", "reason": "주간 검토일 아님", "pairs": []}
            continue
        eligible = candidates
        if policy == "s0_rank_buffer":
            entry_k = engine.rank_slots(n, CHALLENGER["entry_top_pct"])
            eligible = [s for s in candidates if s["rank"] is not None and s["rank"] <= entry_k]
        weak = []
        for h in held.values():
            s = sigs.get(h["ticker"])
            if s is None:  # 판단 불가능한 보유는 유지한다.
                continue
            if policy == "champion_rotation_proxy":
                if rp["only_cooled"] and engine.is_buy(s["kind"]):
                    continue
                if h["calendar_days_held"] is not None and h["calendar_days_held"] < rp["min_hold_days"]:
                    continue
                pnl = h["price"] / h["avg_price"] - 1 if h["avg_price"] > 0 else 0
                if pnl < rp["max_loss_pct"]:
                    continue
            else:
                hold_k = engine.rank_slots(n, CHALLENGER["hold_top_pct"])
                if h["hold_sessions"] is None or h["hold_sessions"] < CHALLENGER["min_hold_sessions"]:
                    continue
                # 보유 완충구간: BUY가 아니어도 순위가 유지되면 교체하지 않는다.
                if s["rank"] is None or s["rank"] <= hold_k:
                    continue
            weak.append((s["score"], h["ticker"], h, s))
        weak.sort(key=lambda x: (x[0], x[1]))
        pairs = []
        for candidate in eligible:
            if not weak or len(pairs) >= rp["max_per_run"]:
                break
            score, ticker, holding, old_signal = weak[0]
            if policy == "champion_rotation_proxy" and candidate["score"] - score < rp["min_gap"]:
                break
            # Challenger도 우위 순위가 없으면 거래하지 않는다. 점수 차는 예상 수익 아님.
            if policy == "s0_rank_buffer" and (candidate["rank"] is None or
                    old_signal["rank"] is None or candidate["rank"] >= old_signal["rank"]):
                break
            pairs.append({"out": ticker, "in": candidate["ticker"],
                          "out_score": score, "in_score": candidate["score"],
                          "out_rank": old_signal["rank"], "in_rank": candidate["rank"],
                          "out_qty": holding["qty"], "out_close": holding["price"],
                          "in_close": candidate["price"]})
            weak.pop(0)
        results[policy] = {"action": "rotate" if pairs else "hold",
                           "reason": "후보와 교체 가능 보유가 정책 문턱을 통과" if pairs else "적격 교체 없음",
                           "pairs": pairs}
    return results


def capture(market: str, now: dt.datetime) -> dict:
    """완료 세션과 PIT/가격 날짜가 일치할 때만 레퍼런스 계좌별 최초 관측을 저장."""
    if market not in ("kr", "us") or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market and timezone-aware now required")
    session = market_clock.latest_completed_session(market, now)
    if not session or market_clock.is_open(market, now):
        return {"saved": 0, "reason": "완료된 폐장 세션 없음"}
    # 국내 16:30 이후/미국은 기존 독립 US PIT 스냅샷 이후. 종가 확정 메타데이터는 여전히 미검증.
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if age < dt.timedelta(hours=1):
        return {"saved": 0, "reason": "마감 후 1시간 대기"}
    max_age_hours = 20 if market == "us" else 12  # US 조기마감 뒤 KST 15:40 스냅샷 게이트까지 허용
    if age > dt.timedelta(hours=max_age_hours):
        return {"saved": 0, "reason": "완료 세션 관측 지연 — 과거 보유를 현재 잔고로 재구성 금지"}
    if market == "us" and db.kv_get("us_signal_snapshot_session") != session:
        return {"saved": 0, "reason": "미국 PIT 스냅샷 세션 불일치"}
    if all(db.rotation_shadow_exists(uid, market, session) for uid in REFERENCE_BOTS):
        return {"saved": 0, "session": session, "reason": "이미 동결됨"}
    history = store.load_signal_history(market)
    if history.empty or "date" not in history.columns:
        return {"saved": 0, "reason": "PIT 시그널 없음"}
    rows = history[history["date"].astype(str) == session]
    if rows.empty or "bar_asof" not in rows.columns or "session_valid" not in rows.columns:
        return {"saved": 0, "reason": "해당 세션 PIT 메타데이터 없음"}
    prices, dates = store.load_portfolio_close_bundle(market)
    if rows["ticker"].duplicated().any():
        return {"saved": 0, "reason": "PIT 행 날짜/세션 정합성 실패"}
    valid = rows[(rows["bar_asof"].astype(str) == session) & (rows["session_valid"] == True)]  # noqa: E712
    entry_k = engine.rank_slots(len(rows), CHALLENGER["entry_top_pct"])
    stale = rows.drop(valid.index)
    if valid.empty or len(valid) / len(rows) < 0.95 or any(
            engine.is_buy(str(r.get("kind"))) and
            ((rank := _finite(r.get("rank"))) is None or rank <= entry_k)
            for r in stale.to_dict("records")):
        return {"saved": 0, "reason": "PIT 가격 정렬 95% 미달 또는 상위 신규 후보 가격 결손"}
    signals = []
    excluded = [str(t) for t in stale["ticker"]]
    for r in valid.to_dict("records"):
        ticker = str(r["ticker"])
        score = _finite(r.get("score"))
        rank = _finite(r.get("rank"))
        price = _finite(prices[ticker][-1]) if prices.get(ticker) else None
        if score is None or not dates.get(ticker) or dates[ticker][-1] != session or price is None or price <= 0:
            if engine.is_buy(str(r.get("kind"))) and (rank is None or rank <= entry_k):
                return {"saved": 0, "reason": "상위 신규 후보 가격 결손"}
            excluded.append(ticker)
            continue
        signals.append({"ticker": ticker, "score": score, "kind": str(r["kind"]),
                        "rank": int(rank) if rank is not None and rank > 0 else None,
                        "event_risk": bool(r.get("event_risk")), "price": price})
    if len(signals) / len(rows) < 0.95:
        return {"saved": 0, "reason": "PIT/원시 종가 최종 정렬 95% 미달"}
    saved = 0
    account_seen = 0
    warned = sorted(store.load_warned_tickers()) if market == "kr" else []
    assumptions = execution.cost_assumptions(market)
    for uid, style in REFERENCE_BOTS.items():
        if db.rotation_shadow_exists(uid, market, session):
            continue
        raw = db.kv_get(f"paper_account:{uid}" + (f":{market}" if market == "us" else ""))
        account = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(account, dict) or not isinstance(account.get("positions"), dict):
            continue  # 시작 계좌가 없으면 가상의 초기 자본을 만들지 않는다.
        account_seen += 1
        cash = _finite(account.get("cash"))
        if cash is None or cash < 0:
            continue
        positions = {p["ticker"]: p for p in db.bot_positions_all(uid, market)}
        holdings = []
        for ticker, position in account["positions"].items():
            if ticker not in dates or dates[ticker][-1] != session:
                holdings = []
                break
            price = _finite(prices[ticker][-1])
            avg = _finite(position.get("avg_price"))
            qty = position.get("qty")
            if price is None or price <= 0 or avg is None or avg <= 0 or type(qty) is not int or qty <= 0:
                holdings = []
                break
            entry = positions.get(ticker, {}).get("entry_date")
            try:
                calendar_days = (dt.date.fromisoformat(session) - dt.date.fromisoformat(entry)).days if entry else None
                held_sessions = sum(entry < day <= session for day in dates[ticker]) if entry else None
            except (TypeError, ValueError):
                calendar_days = held_sessions = None
            holdings.append({"ticker": ticker, "qty": qty, "avg_price": avg, "price": price,
                             "entry_date": entry, "calendar_days_held": calendar_days,
                             "hold_sessions": held_sessions})
        if account["positions"] and not holdings:
            continue
        previous = db.rotation_shadow_recent(uid, market, 1)
        review_due = not previous or dt.date.fromisoformat(previous[0]["session"]).isocalendar()[:2] != dt.date.fromisoformat(session).isocalendar()[:2]
        cfg = strategy.bot_params(style)
        total = cash + sum(h["qty"] * h["price"] for h in holdings)
        tranche_alloc = total * cfg["position_pct"] / strategy.entry_tranches(style)
        snapshot = {"version": VERSION, "mode": "shadow", "live_eligible": False,
                    "session": session, "observed_at": now.isoformat(), "market": market,
                    "uid": uid, "style": style, "signal_count": len(rows),
                    "aligned_signal_count": len(signals), "excluded_tickers": sorted(excluded),
                    "signals": signals, "holdings": holdings, "cash": cash,
                    "min_buy_score": cfg["min_buy_score"], "max_positions": cfg["max_positions"],
                    "tranche_alloc": tranche_alloc, "warned": warned,
                    "recent_sold": sorted(_recent_sold(uid, market, style, now)),
                    "cost_assumptions": assumptions, "review_due": review_due,
                    "policies": {"champion_rotation_proxy": strategy.rotation_params(style),
                                 "s0_rank_buffer": CHALLENGER},
                    "limits": "회전 첫 결정의 종가 프록시. 실제 주문·위험청산·신규진입·분할매수·최종 체결과 동일하지 않음. 종가 원천 확정성 미검증."}
        snapshot["decisions"] = decide(snapshot, style, review_due=review_due)
        for decision in snapshot["decisions"].values():
            decision["fixed_orders"] = _fixed_orders(snapshot, decision["pairs"])
        saved += int(db.rotation_shadow_add_once(uid, market, session, snapshot))
    return {"saved": saved, "session": session,
            "reason": (None if saved else "레퍼런스 계좌 없음" if not account_seen
                       else "보유 종가·현금 정합성 미충족 또는 해당 세션 이미 동결")}


def _recent_sold(uid: int, market: str, style: str, now: dt.datetime) -> set[str]:
    horizon = strategy.rotation_params(style)["cooldown_days"] * 86400
    return {t["ticker"] for t in db.bot_trades_recent(uid, 200, market)
            if t["side"] == "sell" and 0 <= now.timestamp() - t["ts"] < horizon}


def _episode_tickers(snapshot: dict) -> set[str]:
    return ({h["ticker"] for h in snapshot["holdings"]} |
            {p["in"] for d in snapshot["decisions"].values() for p in d["pairs"]})


def collect_forward(market: str, now: dt.datetime) -> dict:
    """해당 세션의 최초 관측 종가만 저장하고 과거 가격 수정은 영구 보류한다."""
    if market not in ("kr", "us") or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market and timezone-aware now required")
    session = market_clock.latest_completed_session(market, now)
    if not session or market_clock.is_open(market, now):
        return {"marked": 0, "reason": "완료 세션 없음"}
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if age < dt.timedelta(hours=1) or age > dt.timedelta(hours=20 if market == "us" else 12):
        return {"marked": 0, "reason": "최초 관측 시간창 밖"}
    prices, dates = store.load_portfolio_close_bundle(market)
    maps = {t: dict(zip(dates[t], ps)) for t, ps in prices.items()
            if t in dates and len(dates[t]) == len(ps)}
    marked = halted = gaps = 0
    for uid in REFERENCE_BOTS:
        for episode in db.rotation_shadow_recent(uid, market, 35):
            if episode.get("version") != VERSION or episode["session"] >= session:
                continue
            sessions = market_clock.next_sessions(market, episode["session"], 20)
            if session not in sessions or all(d["action"] == "hold" for d in episode["decisions"].values()):
                continue
            origin = episode["session"]
            if db.rotation_shadow_revision_halt(uid, market, origin):
                continue
            tickers = _episode_tickers(episode)
            existing = db.rotation_shadow_marks(uid, market, origin)
            reference = {h["ticker"]: h["price"] for h in episode["holdings"]}
            reference.update({p["in"]: p["in_close"]
                              for d in episode["decisions"].values() for p in d["pairs"]})
            revised = next((f"{t}:{origin}" for t, price in reference.items()
                            if maps.get(t, {}).get(origin) != price), None)
            if revised is None:
                revised = next((f"{t}:{day}" for day, panel in existing.items()
                                for t, price in panel.items() if maps.get(t, {}).get(day) != price), None)
            if revised:
                db.rotation_shadow_revision_halt(uid, market, origin, revised)
                halted += 1
                continue
            panel = {t: _finite(maps.get(t, {}).get(session)) for t in tickers}
            if not panel or any(p is None or p <= 0 for p in panel.values()):
                gaps += 1
                continue
            marked += db.rotation_shadow_marks_add_once(uid, market, origin, session,
                                                       panel, observed=int(now.timestamp()))
    return {"marked": marked, "revision_halts": halted, "price_gaps": gaps, "session": session}


def score_completed_session(market: str, now: dt.datetime) -> str | None:
    """채점 가능한 최신 세션. 미도래 US/KR 수집 창을 가격 누락으로 오경보하지 않는다."""
    session = market_clock.latest_completed_session(market, now)
    if not session:
        return None
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    too_early = now.astimezone(dt.timezone.utc) < close + dt.timedelta(hours=1)
    if market == "us":
        from zoneinfo import ZoneInfo
        age = now.astimezone(dt.timezone.utc) - close
        too_early = too_early or (age < dt.timedelta(hours=20) and
                                  now.astimezone(ZoneInfo("Asia/Seoul")).time() < dt.time(15, 40))
    return market_clock.previous_session(market, session) if too_early else session


def evaluate(snapshot: dict, marks: dict[str, dict[str, float]], sessions: list[str],
             *, completed_session: str, revision_halt: str | None = None) -> dict:
    """한 번의 동일 초기 자본 비교. 겹치는 에피소드를 합산한 전략 트랙레코드가 아니다."""
    base = {"mode": "shadow", "live_eligible": False, "version": snapshot.get("version"),
            "session": snapshot["session"]}

    def blocked(reason: str, status: str = "blocked") -> dict:
        return {**base, "ready": False, "status": status, "reason": reason}

    if snapshot.get("version") != VERSION:
        return blocked("고정 수량이 없는 이전 정책 버전")
    if revision_halt:
        return blocked("동결 이후 원시 가격 수정/누락: " + revision_halt)
    decisions = snapshot["decisions"]
    if any(d.get("fixed_orders") is None for d in decisions.values()):
        return blocked("판단 시점 비용 포함 정수주 매수 불가")
    if all(not d["fixed_orders"] for d in decisions.values()):
        return blocked("양 정책 모두 유지 — 회전 비교 표본 아님", "no_action")
    if len(sessions) != 20 or not all(a < b for a, b in zip(sessions, sessions[1:])):
        return blocked("거래일 패널 불완전")
    if completed_session < sessions[0]:
        return blocked("다음 거래일 종가 대기", "pending")
    observed = []
    for day in sessions:
        if day > completed_session:
            break
        panel = marks.get(day)
        if panel is None:
            return blocked("완료 거래일 최초 관측 가격 누락: " + day)
        if any(t not in panel or _finite(panel[t]) is None or panel[t] <= 0
               for t in _episode_tickers(snapshot)):
            return blocked("완료 거래일 종목 가격 누락: " + day)
        observed.append(day)
    held = {h["ticker"]: h["qty"] for h in snapshot["holdings"]}
    initial = snapshot["cash"] + sum(h["qty"] * h["price"] for h in snapshot["holdings"])
    if initial <= 0 or not math.isfinite(initial):
        return blocked("초기 자산 오류")
    metrics = {}
    paths = {}
    for policy, decision in decisions.items():
        cash, positions = float(snapshot["cash"]), dict(held)
        costs = traded = 0.0
        path = []
        peak, mdd = initial, 0.0
        for i, day in enumerate(observed):
            panel = marks[day]
            if i == 0:
                for order in decision["fixed_orders"]:
                    ticker, side, qty = order["ticker"], order["side"], order["qty"]
                    if side == "sell" and positions.get(ticker, 0) < qty:
                        return blocked("고정 매도 수량 보유 초과")
                    fill = execution.calculate(panel[ticker], qty, side, snapshot["market"],
                                               assumptions=snapshot["cost_assumptions"])
                    cash += fill.cash_change
                    if cash < -1e-7:
                        return blocked("다음 거래일 가격 갭으로 고정 수량 매수 불가")
                    positions[ticker] = positions.get(ticker, 0) + (qty if side == "buy" else -qty)
                    costs += fill.total_fees + fill.slippage_cost
                    traded += fill.gross_notional
            nav = cash + sum(q * panel[t] for t, q in positions.items())
            if not math.isfinite(nav) or nav < 0:
                return blocked("평가자산 오류")
            peak = max(peak, nav)
            mdd = min(mdd, (nav / peak - 1) * 100)
            path.append({"date": day, "nav": round(nav, 8),
                         "return_pct": round((nav / initial - 1) * 100, 6),
                         "max_drawdown_pct": round(mdd, 6)})
        paths[policy] = path
        metrics[policy] = {"orders": len(decision["fixed_orders"]), "traded_notional_pct": round(traded / initial * 100, 6),
                           "cost_drag_pct": round(costs / initial * 100, 6),
                           "h5": path[4] if len(path) >= 5 else None,
                           "h20": path[19] if len(path) >= 20 else None}
    deltas = {}
    for horizon in ("h5", "h20"):
        a, b = metrics["s0_rank_buffer"][horizon], metrics["champion_rotation_proxy"][horizon]
        deltas[horizon] = (round(a["return_pct"] - b["return_pct"], 6) if a and b else None)
    return {**base, "ready": True, "status": "complete" if len(observed) == 20 else "observing",
            "complete": len(observed) == 20, "completed_sessions": len(observed),
            "target_sessions": 20, "initial_value": round(initial, 8),
            "metrics": metrics, "delta_net_pp": deltas, "paths": paths,
            "note": "다음 거래일 종가에 판단 시 확정한 수량을 가상 체결. 배당·기업행동·호가·실제 체결 미반영; 겹치는 에피소드 수익을 합산하지 않음."}
