"""Offline R3 intent restoration and paired price-only execution diagnostic.

Old intent has actions, not submitted quantities. NEW quantities are locked at
the common close; next-open old fills restore only the old account state.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core
from signal_desk.signals.research_net_alpha_execution import ExecutionBook
from signal_desk.signals.research_decision import (
    Candidate, CompletedDailyBar, DailyPolicy, DailySnapshot, PortfolioState,
    Position, ResearchClock, decide,
)

MODEL = "data/research/kr-ridge-r3-model-2026-10-17-v1.json"
RESULT = "data/research/kr-ridge-r3-result-2026-10-17-v4.json"
EXPECTED = {
    MODEL: "81de9f736b6c3e853bfa7a8a30009551aa5757783e50adf8128e2726d737fdd0",
    RESULT: "2cf98e96eff44d415c0264bd1ec1b6f2b036804b9e2bbeb28476dc807994abfd",
    "docs/kr-ridge-selection-plan-2026-10-17.json":
        "5bf03e849b6503332b3eb9468e69be5b3d3691f21c6c50819f44c71e5d50417f",
}
EXECUTION_CODE = {
    "src/signal_desk/signals/research_net_alpha.py":
        "d0398de4c200ca2e59a1eb1e165c2805e94696314306e7d5532137fead67e70a",
    "src/signal_desk/signals/research_net_alpha_execution.py":
        "3d6c941185f7a8c35b77527dd49da240357751d1a2241ff4de48cf5957960625",
}
CONTRACT = {
    "id": "kr-r3-h5-netting-devdiag-v1", "grade": "development_diagnostic",
    "integration_branch": "codex/r3-turnover-diagnostic",
    "base_commit": "576b55ae7d74374ea4f964deac1fc771ca58f200",
    "decision_start": "2024-10-02", "decision_end": "2024-12-30",
    "liquidation_through": "2025-01-31", "decision_count": 61,
    "phases": 5, "initial_cash_krw": 10_000_000, "max_positions": 6,
    "scenario": "75bp_full", "side_cost": 75 / 20_000,
    "reference": "same-phase original price-account close equity on decision day / 6",
    "reference_timing": "completed close t; no future open, fill, NAV or session volume",
    "quantity_rule": "core.orders: buy floor(shortfall/(close*1.01*1.006)); "
        "sell ceil(excess/close), capped at holding and 1% known decision-day volume",
    "budget_rule": "common reference NAV and 1/6 per selected name; never each arm NAV",
    "execution_rule": "ExecutionBook sells first; next regular open proxy; fill fees only; "
        "T+2 signed settlement credit is simulated buying power, not withdrawable cash",
    "max_paths": 10, "new_collection": 0, "max_seconds": 600,
    "quantity_provenance": "NEW close-locked quantities; never inferred from old fill quantity",
    "valuation_scope": "raw-price execution only; rights completeness unverified",
    "rights": "LIST_SHRS stability is not corporate-action or rights certification",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked_inputs(root):
    hashes = {name: digest(root / name) for name in EXPECTED}
    if hashes != EXPECTED:
        raise ValueError("fixed_input_hash_mismatch")
    return (json.loads((root / MODEL).read_text()),
            json.loads((root / RESULT).read_text()),
            json.loads((root / "docs/kr-ridge-selection-plan-2026-10-17.json").read_text()), hashes)


def reconcile_saved_rows(result, cfg):
    """Re-rank every saved prediction; this alone is not feature reproduction."""
    dates = result["evaluation_decision_dates"]
    first, last = map(dt.date.fromisoformat, (CONTRACT["decision_start"], CONTRACT["decision_end"]))
    expected_dates = [(first + dt.timedelta(days=i)).isoformat() for i in range((last-first).days+1)
                      if market_clock.is_session("kr", first + dt.timedelta(days=i))]
    if dates != expected_dates or len(dates) != 61 or set(result["selections"]) != set(dates):
        raise ValueError("decision_calendar_mismatch")
    grouped, seen = defaultdict(list), set()
    for row in result["predictions"]:
        key = row["date"], row["ticker"]
        if key in seen or key[0] not in dates or not math.isfinite(row["predicted_gross"]):
            raise ValueError("invalid_or_duplicate_prediction")
        seen.add(key)
        grouped[key[0]].append((key[1], row["predicted_gross"]))
    for day in dates:
        row = result["selections"][day]
        ranked = sorted(grouped[day], key=lambda p: (-p[1], p[0]))
        selected = [[t, p] for t, p in ranked[:cfg["max_positions"]]
                    if p > cfg["predicted_gross_entry_min"]]
        if len(ranked) < cfg["min_price_feature_names"]:
            selected = []
        if len(ranked) != row["feature_names"] or selected != row["selected"]["ridge"]:
            raise ValueError("saved_prediction_selection_mismatch:" + day)
    paths = result["scenarios"]["75bp_full"]["ridge"]
    if len(paths) != 5:
        raise ValueError("phase_count_mismatch")
    for phase, path in enumerate(paths):
        for fill in path["fills"]:
            _, arm, phase_text, day, plan_hash = fill["decision_input_id"].split("/")
            if (arm != "ridge" or int(phase_text) != phase
                    or plan_hash != result["plan_sha256"]
                    or not market_clock.consecutive_sessions("kr", day, fill["session"])
                    or fill["session"] > CONTRACT["liquidation_through"]):
                raise ValueError("saved_fill_intent_link_mismatch")
            # Exit decisions carry their current close snapshot ID, not the
            # original entry selection; liquidation tail dates have no new picks.
            if fill["side"] == "buy" and (day not in dates or dates.index(day) % 5 != phase
                    or fill["ticker"] not in {t for t, _ in result["selections"][day]["selected"]["ridge"]}):
                raise ValueError("saved_buy_selection_link_mismatch")
    return {"dates": len(dates), "prediction_rows": len(seen),
            "selection_rows": sum(len(result["selections"][d]["selected"]["ridge"]) for d in dates),
            "scope": "saved predictions/selections/fill-origin links; not pre-open quantity intent"}


def reproduce_fixed_predictions(root, model, result, cfg, *, deadline=None):
    """Optional controller-side preflight only: loaders/features/predict, no fit/run.

    Checks all prediction rows, including nonselected names, and raw manifests.
    """
    from scripts.measure import kr_ridge_selection_lab as lab
    from scripts.measure.price_challenger_lab import PricePanel
    from signal_desk.signals import research_ridge

    if digest(Path(research_ridge.__file__)) != model["fit_module_sha256"]:
        raise ValueError("frozen_predict_code_mismatch")
    for name, sha in result["code_sha256"].items():
        if digest(root / name) != sha:
            raise ValueError("frozen_code_mismatch:" + name)
    if lab.digest(lab.FINANCIAL) != result["financial_archive_sha256"]:
        raise ValueError("financial_input_hash_mismatch")
    days, sources = lab.load_krx_archives(
        [root / "data/research" / s["archive"] for s in result["sources"]])
    if sources != result["sources"]:
        raise ValueError("price_source_hash_mismatch")
    days = {d: rows for d, rows in days.items() if d <= cfg["liquidate_through"]}
    by_day = {d: {r["ISU_CD"]: r for r in rows} for d, rows in days.items()}
    panel = PricePanel(days)
    financials = lab.load_financials(lab.FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in lab.top200(days[cfg["financial_roster_asof"]])}
    expected = {(r["date"], r["ticker"]): r["predicted_gross"] for r in result["predictions"]}
    actual = {}
    for day in result["evaluation_decision_dates"]:
        check_time(deadline)
        rows, *_ = lab.features_at(day, panel, by_day, financials, roster)
        prediction = research_ridge.predict([r["features"] for r in rows], model["model"]) if rows else []
        actual.update({(day, r["ticker"]): float(p) for r, p in zip(rows, prediction, strict=True)})
    if actual.keys() != expected.keys() or any(
            not math.isclose(actual[k], expected[k], rel_tol=1e-12, abs_tol=1e-14) for k in expected):
        raise ValueError("frozen_feature_prediction_reproduction_mismatch")
    for day in result["evaluation_decision_dates"]:
        ranked = sorted(((t, p) for (d, t), p in actual.items() if d == day), key=lambda r: (-r[1], r[0]))
        chosen = [t for t, p in ranked[:cfg["max_positions"]] if p > cfg["predicted_gross_entry_min"]]
        if len(ranked) < cfg["min_price_feature_names"]:
            chosen = []
        if chosen != [t for t, _ in result["selections"][day]["selected"]["ridge"]]:
            raise ValueError("reproduced_selection_rank_mismatch:"+day)
    return ({"state": "matched", "rows": len(actual), "sources": sources},
            by_day, panel)


def check_time(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise ValueError("diagnostic_600_second_limit")


def number(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def checked_reviewed(root, result, by_day, panel):
    from scripts.measure.kr_ridge_action_sources import reviewed_facts
    path = root / "data/research/kr-ridge-r3-listing-originals-2026-10-17-v1.zip"
    if digest(path) != result["listing_archive_sha256"]:
        raise ValueError("listing_original_hash_mismatch")
    facts = reviewed_facts(path, by_day, panel)
    encoded = [{**asdict(f), "source_observed_at": f.source_observed_at.isoformat()}
               for f in facts.values()]
    if encoded != result["reviewed_share_changes"] or len(facts) != 8:
        raise ValueError("reviewed_KIND_facts_mismatch")
    return facts


def observed_gap(day, names, by_day, panel, reviewed, *, stage="both"):
    """Partial source validation, never a certificate of absent rights/events."""
    previous = panel.sessions[panel.index[day]-1]
    for ticker in sorted(names):
        row = by_day[day].get(ticker, {})
        opened, closed = panel.bars.get((day, ticker), (None, None))
        before = number(by_day[previous].get(ticker, {}).get("LIST_SHRS"))
        after = number(row.get("LIST_SHRS"))
        field = None
        if stage in ("both", "open") and (not opened or opened <= 0):
            field = "open_or_halt"
        elif stage in ("both", "close") and (not closed or closed <= 0):
            field = "close"
        elif before is None or after is None or before <= 0 or after <= 0:
            field = "LIST_SHRS_unit_unknown"
        elif before != after:
            fact = reviewed.get((day, ticker))
            if (fact is None or fact.holder_quantity_effect != "unchanged"
                    or fact.listed_shares_before != before or fact.listed_shares_after != after):
                field = "unreviewed_unit_change_or_holder_rights"
        if field:
            return {"session": day, "ticker": ticker, "field": field,
                    "last_checked": previous, "reason": "source_gap:"+day+":"+ticker+":"+field}
    return None


def restore_old_intents(result, cfg, by_day, panel, reviewed, *, deadline=None):
    """Restore t state from fills <=t, then use the original pure decide().

    This is an intent/equity reconciliation, not another old execution engine.
    Every saved fill must link to a previous action; quantities never enter an
    earlier decision. Old price-account cash uses its original immediate credit.
    """
    sessions = [d for d in panel.sessions if cfg["evaluation_start"] <= d <= cfg["liquidate_through"]]
    restored = []
    for phase, path in enumerate(result["scenarios"]["75bp_full"]["ridge"]):
        policy = DailyPolicy(f"{cfg['candidate_id']}:ridge:phase{phase}", "r3-75bp",
                             cfg["max_positions"], cfg["horizon_open_to_open_sessions"])
        positions, used, cash, previous_actions = {}, set(), float(cfg["starting_cash"]), ()
        by_session = defaultdict(list)
        for fill in path["fills"]:
            by_session[fill["session"]].append(fill)
        equity_rows = path["equity_curve"]
        if [r["session"] for r in equity_rows] != sessions:
            raise ValueError(f"old_equity_calendar_mismatch:phase{phase}")
        rows = []
        for index, day in enumerate(sessions):
            check_time(deadline)
            todays = by_session.pop(day, [])
            names = set(positions) | {f["ticker"] for f in todays}
            gap = observed_gap(day, names, by_day, panel, reviewed)
            if gap:
                raise ValueError(f"old_intent_source_gap:phase{phase}:"+gap["reason"])
            for ticker in positions:
                fact = reviewed.get((day, ticker))
                if fact is not None:
                    if positions[ticker].listed_shares != fact.listed_shares_before:
                        raise ValueError(f"old_held_share_unit:{day}:{ticker}")
                    positions[ticker] = replace(positions[ticker], listed_shares=fact.listed_shares_after)
            matched = set()
            for fill in todays:
                ticker, quantity, price = fill["ticker"], fill["shares"], fill["price"]
                action = "ENTER" if fill["side"] == "buy" else "EXIT"
                candidates = [a for a in previous_actions if a.ticker == ticker and a.action == action
                              and a.input_id == fill["decision_input_id"] and a.valid_until == day]
                if len(candidates) != 1 or (ticker, action) in matched:
                    raise ValueError(f"old_fill_action_link:phase{phase}:{day}:{ticker}:{action}")
                intent = candidates[0]
                matched.add((ticker, action))
                if (type(quantity) is not int or quantity <= 0
                        or price != panel.bars[day, ticker][0]):
                    raise ValueError(f"old_fill_price_quantity:phase{phase}:{day}:{ticker}")
                if action == "ENTER":
                    if ticker in positions or intent.thesis_id in used:
                        raise ValueError(f"old_entry_state:{day}:{ticker}")
                    cash -= quantity * price * (1+CONTRACT["side_cost"])
                    shares = int(number(by_day[day][ticker]["LIST_SHRS"]))
                    positions[ticker] = Position(ticker, quantity, price, index, intent.thesis_id, shares)
                    used.add(intent.thesis_id)
                else:
                    old = positions.get(ticker)
                    if old is None or quantity > old.shares:
                        raise ValueError(f"old_exit_state:{day}:{ticker}")
                    cash += quantity * price * (1-CONTRACT["side_cost"])
                    if quantity == old.shares:
                        del positions[ticker]
                    else:
                        positions[ticker] = replace(old, shares=old.shares-quantity, exit_requested=True)
            equity = cash + sum(p.shares * panel.bars[day, t][1] for t, p in positions.items())
            if cash < -1e-6 or not math.isclose(equity, equity_rows[index]["equity"], rel_tol=0, abs_tol=1e-5):
                raise ValueError(f"old_close_equity:phase{phase}:{day}:equity/cash")
            picked = result["selections"].get(day, {}).get("selected", {}).get("ridge", [])
            active = day in result["evaluation_decision_dates"] and result["evaluation_decision_dates"].index(day) % 5 == phase
            picks = picked if active else []
            names = set(positions) | {t for t, _ in picks}
            stamp = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(16), ZoneInfo("Asia/Seoul"))
            bars = []
            for ticker in sorted(names):
                opened, closed = panel.bars.get((day, ticker), (None, None))
                bars.append(CompletedDailyBar("kr", day, ticker,
                    opened if opened and opened > 0 else None, closed if closed and closed > 0 else None,
                    stamp, f"saved-price:{day}:{ticker}",
                    status="trading" if opened and closed and number(by_day[day].get(ticker, {}).get("LIST_SHRS")) else "unknown"))
            snapshot = DailySnapshot("kr", day, stamp, f"r3/ridge/{phase}/{day}/{result['plan_sha256']}",
                tuple(bars), tuple(Candidate(t, rank+1, score, True, f"ridge:{phase}:{day}:{t}")
                                   for rank, (t, score) in enumerate(picks)))
            previous_actions = decide(snapshot, PortfolioState("kr", cash, tuple(positions.values()), frozenset(used)),
                policy, ResearchClock(stamp, index, market_clock.next_sessions("kr", day, 1)[0]))
            rows.append({"session": day, "reference_equity": equity_rows[index]["equity"],
                         "cash": cash, "holdings": {t: p.shares for t, p in positions.items()},
                         "actions": [asdict(a) for a in previous_actions]})
        if by_session:
            raise ValueError(f"old_fill_outside_calendar:phase{phase}")
        restored.append(rows)
    return restored


def saved_fill_baseline(paths):
    """Arithmetic on saved fills only; never a new arm or claimed saving."""
    rows = []
    for phase, path in enumerate(paths):
        by_key = defaultdict(lambda: {"buy": [], "sell": []})
        buy = sell = 0.0
        for f in path["fills"]:
            if (f["side"] not in ("buy", "sell") or type(f["shares"]) is not int
                    or f["shares"] <= 0 or not math.isfinite(f["price"]) or f["price"] <= 0):
                raise ValueError("invalid_saved_fill")
            amount = f["shares"] * f["price"]
            buy += amount if f["side"] == "buy" else 0
            sell += amount if f["side"] == "sell" else 0
            by_key[f["session"], f["ticker"]][f["side"]].append(f)
        overlap = []
        for (day, ticker), sides in sorted(by_key.items()):
            if not sides["buy"] or not sides["sell"]:
                continue
            prices = {f["price"] for fs in sides.values() for f in fs}
            shares = min(sum(f["shares"] for f in sides["buy"]), sum(f["shares"] for f in sides["sell"]))
            cost = 2 * shares * next(iter(prices)) * CONTRACT["side_cost"] if len(prices) == 1 else None
            overlap.append({"session": day, "ticker": ticker, "overlap_shares": shares,
                "same_price": len(prices) == 1, "two_sided_assumed_cost_krw": cost,
                "reason": None if cost is not None else "unequal_saved_fill_prices"})
        rows.append({"phase": phase, "buy_principal_krw": buy, "sell_principal_krw": sell,
            "turnover_initial_cash": (buy + sell) / CONTRACT["initial_cash_krw"],
            "assumed_fill_cost_krw": (buy + sell) * CONTRACT["side_cost"],
            "overlap_count": len(overlap), "overlaps": overlap,
            "overlap_assumed_cost_krw": sum(o["two_sided_assumed_cost_krw"] for o in overlap)
                if all(o["same_price"] for o in overlap) else None,
            "scope": "saved v4 fill arithmetic, not certified rights/settlement or policy benefit"})
    return rows


@dataclass(frozen=True)
class CommonDecision:
    day: str
    reference_nav: float
    selected: tuple  # (ticker, rank); locked before either arm submits
    closes: tuple
    volumes: tuple
    weight_per_name: float = 1 / 6

    def __post_init__(self):
        if (not math.isfinite(self.reference_nav) or self.reference_nav <= 0
                or not 0 < self.weight_per_name <= 1
                or not all(isinstance(v, tuple) for v in (self.selected, self.closes, self.volumes))):
            raise ValueError("invalid_frozen_decision")


def submit_pair(books, decision):
    """Use exactly one close reference; do not recompute from either arm NAV."""
    if set(books) != {"forced", "net"} or books["forced"] is books["net"]:
        raise ValueError("two_independent_books_required")
    target = {t: decision.weight_per_name for t, _ in decision.selected}
    priority = {t: -rank for t, rank in decision.selected}
    if len(target) != len(decision.selected) or sum(target.values()) > 1 + 1e-12:
        raise ValueError("invalid_common_selection")
    # Validate both sets of inputs before mutating either residual queue.
    for b in books.values():
        core.orders(target, b.holdings, dict(decision.closes), dict(decision.volumes),
                    decision.reference_nav, b.spec)
    return {arm: b.submit(decision.day, target, dict(decision.closes), dict(decision.volumes),
        decision.reference_nav, priority=priority, input_id="r3-devdiag/" + decision.day,
        budget=1, force_roundtrip=arm == "forced") for arm, b in books.items()}


def reconcile_book(book, initial):
    """Independent signed-fill reconciliation against an initial book snapshot."""
    holdings = dict(initial["holdings"])
    buying_power = initial["buying_power"]
    fills = book.fills[len(initial["fills"]):]
    for f in fills:
        sign = 1 if f["side"] == "buy" else -1
        holdings[f["ticker"]] = holdings.get(f["ticker"], 0) + sign * f["shares"]
        buying_power -= sign * f["shares"] * f["price"] + f["fee"]
    holdings = {t: q for t, q in holdings.items() if q}
    if (holdings != book.holdings or any(q < 0 for q in holdings.values())
            or not math.isclose(buying_power, book.buying_power, abs_tol=1e-6)
            or not math.isclose(book.fees - initial["fees"], sum(f["fee"] for f in fills), abs_tol=1e-6)):
        raise ValueError("cash_quantity_fee_reconciliation_failed")
    return True


def book_metrics(book, closes, *, rights_reason=None):
    """Cash, quantity and exposure stay separate; absent marks are never zero."""
    state = book.state()
    marked = all(t in closes and math.isfinite(closes[t]) and closes[t] > 0 for t in book.holdings)
    state.update({"buy_principal_krw": sum(f["shares"] * f["price"] for f in book.fills if f["side"] == "buy"),
        "sell_principal_krw": sum(f["shares"] * f["price"] for f in book.fills if f["side"] == "sell"),
        "exposure_krw": sum(q * closes[t] for t, q in book.holdings.items()) if marked and not rights_reason else None,
        "price_nav_krw": book.price_nav(closes) if marked and not rights_reason else None,
        "uncomputed_reason": rights_reason or (None if marked else "missing_held_price"),
        "total_return": None, "net_return": None, "mdd": None, "adoption": None})
    state["turnover_initial_cash"] = (state["buy_principal_krw"] + state["sell_principal_krw"]) / book.spec["initial_cash_krw"]
    return state


def execution_spec(cfg):
    return {"scenarios": ["75bp_full"], "initial_cash_krw": cfg["starting_cash"],
        "evaluation_start": cfg["evaluation_start"], "evaluation_end": cfg["evaluation_end"],
        "liquidation_through": cfg["liquidate_through"], "max_positions": cfg["max_positions"],
        "buy_limit_above_close": .01, "decision_one_way_cost": .006,
        "prior_volume_participation": .01, "sell_residual_attempts": 5}


def reconciled_selection(day, result, old_row):
    """A saved rank alone is not an entry: require the restored action too."""
    picked = result["selections"][day]["selected"]["ridge"]
    enters = [a for a in old_row["actions"] if a["action"] == "ENTER"]
    expected = [(t, rank + 1) for rank, (t, _) in enumerate(picked)]
    if (old_row["session"] != day
            or [(a["ticker"], a["candidate_rank"]) for a in enters] != expected
            or any(a["valid_until"] != market_clock.next_sessions("kr", day, 1)[0]
                   or a["horizon_sessions"] != 5 or a["risk_budget_fraction"] != 1
                   for a in enters)
            or any(a["action"] in ("WAIT", "HOLD") for a in old_row["actions"])):
        raise ValueError("restored_action_selection_mismatch:" + day)
    return picked


def replay_pair(phase, result, cfg, by_day, panel, reviewed, old_rows, *, deadline=None):
    """Two existing ExecutionBooks, common original close reference and schedule."""
    books = {a: ExecutionBook(execution_spec(cfg), "75bp_full") for a in ("forced", "net")}
    initial = {a: b.state() for a, b in books.items()}
    scheduled = result["evaluation_decision_dates"][phase::5]
    terminal = market_clock.next_sessions("kr", scheduled[-1], 5)[-1]
    reference = {r["session"]: r["reference_equity"] for r in old_rows}
    restored = {r["session"]: r for r in old_rows}
    selections = {d: reconciled_selection(d, result, restored[d]) for d in scheduled}
    sessions = [d for d in panel.sessions if cfg["evaluation_start"] <= d <= cfg["liquidate_through"]]
    trace, submissions, gap, last_marks = [], [], None, {}
    for day in sessions:
        check_time(deadline)
        if gap:
            trace.append({"session": day, "forced": None, "net": None, "reason": gap["reason"]})
            continue
        names = set().union(*(set(b.holdings) | {o["ticker"] for o in b.pending} for b in books.values()))
        gap = observed_gap(day, names, by_day, panel, reviewed, stage="open")
        if gap:
            trace.append({"session": day, "forced": None, "net": None, "reason": gap["reason"]})
            continue
        opens = {t: {"price": panel.bars[day, t][0], "open_tradable": True,
                     "source_id": f"preserved-raw:{day}:{t}"} for t in names}
        for a, b in books.items():
            b.open_session(day, opens)
            reconcile_book(b, initial[a])
        picked = selections.get(day, [])
        names = set().union(*(set(b.holdings) for b in books.values())) | {t for t, _ in picked}
        gap = observed_gap(day, names, by_day, panel, reviewed, stage="close")
        if gap:
            # Current open fills remain inspectable but this close and all later
            # monetary marks are unavailable for both arms.
            trace.append({"session": day, "forced": None, "net": None, "reason": gap["reason"]})
            continue
        closes = {t: panel.bars[day, t][1] for t in names}
        volumes = {t: number(by_day[day][t].get("ACC_TRDVOL")) for t in names}
        if day in scheduled or day == terminal:
            if any(v is None or v < 0 for v in volumes.values()):
                ticker = next(t for t, v in volumes.items() if v is None or v < 0)
                gap = {"session": day, "ticker": ticker, "field": "decision_volume",
                       "last_checked": day, "reason": f"source_gap:{day}:{ticker}:decision_volume"}
                trace.append({"session": day, "forced": None, "net": None, "reason": gap["reason"]})
                continue
            decision = CommonDecision(day, reference[day],
                tuple((t, rank+1) for rank, (t, _) in enumerate(picked)),
                tuple(sorted(closes.items())), tuple(sorted(volumes.items())))
            orders = submit_pair(books, decision)
            submissions.append({"common": asdict(decision), "orders": orders,
                                "terminal": day == terminal})
        trace.append({"session": day, **{a: {
            "settled_cash": b.settled_cash, "settlements": deepcopy(b.settlements),
            "buying_power": b.buying_power, "holdings": dict(b.holdings),
            "fees": b.fees, "exposure_krw": sum(q*closes[t] for t, q in b.holdings.items()),
            "price_nav_krw": b.price_nav(closes)} for a, b in books.items()}})
        last_marks = closes
    arms = {}
    for a, b in books.items():
        reconcile_book(b, initial[a])
        metrics = book_metrics(b, last_marks, rights_reason=gap["reason"] if gap else None)
        # Retain the known prefix as a separate diagnostic, never a zero-filled
        # final result after a source stop.
        prefix = {k: metrics[k] for k in ("buy_principal_krw", "sell_principal_krw", "turnover_initial_cash")}
        prefix["assumed_fill_cost_krw"] = b.fees
        overlap = saved_fill_baseline([{"fills": [{**f, "session": f["on"]} for f in b.fills]}])[0]
        prefix["same_session_roundtrips"] = overlap["overlaps"]
        prefix["through"] = b.last_open
        if gap:
            for k in ("buy_principal_krw", "sell_principal_krw", "turnover_initial_cash", "fees",
                      "settled_cash", "buying_power", "settlements", "holdings", "pending"):
                metrics[k] = None
        arms[a] = {"metrics": metrics, "confirmed_execution_prefix": prefix,
                   "fills": deepcopy(b.fills), "events": deepcopy(b.events),
                   "unresolved_orders_at_stop": deepcopy(b.pending)}
    comparable = [r for r in trace if r["forced"] is not None and r["net"] is not None]
    last = comparable[-1] if comparable else None
    delta = None if last is None else {
        "through": last["session"], "cost_saved_krw": last["forced"]["fees"]-last["net"]["fees"],
        "price_nav_difference_krw": last["net"]["price_nav_krw"]-last["forced"]["price_nav_krw"],
        "price_pnl_difference_before_cost_krw":
            last["net"]["price_nav_krw"]+last["net"]["fees"]
            -last["forced"]["price_nav_krw"]-last["forced"]["fees"],
        "exposure_difference_krw": last["net"]["exposure_krw"]-last["forced"]["exposure_krw"],
        "buying_power_difference_krw": last["net"]["buying_power"]-last["forced"]["buying_power"],
        "settled_cash_difference_krw": last["net"]["settled_cash"]-last["forced"]["settled_cash"],
        "strategy_improvement": None}
    if delta:
        delta["improvement_claim"] = ("rejected_price_pnl_deterioration"
            if delta["price_pnl_difference_before_cost_krw"] < -1e-6
            else "not_established_exposure_and_cash_differ" if any(abs(delta[k]) > 1e-6 for k in (
                "exposure_difference_krw", "buying_power_difference_krw", "settled_cash_difference_krw"))
            else "not_established_rights_unverified")
    return {"phase": phase, "state": "partial_source_stop" if gap else "price_only_complete",
        "liquidation_state": "unknown_after_stop" if gap else "unmatured_residual"
            if any(b.holdings or b.pending or b.settlements for b in books.values()) else "simulated_closed",
        "terminal_close": terminal, "stop": gap, "submissions": submissions,
        "trace": trace, "arms": arms, "comparable_prefix_difference": delta,
        "total_return": None, "net_return": None, "mdd": None, "adoption": None}


def diagnose(root):
    """Controller-only one-shot offline diagnostic; never call R3 run()/fit()."""
    deadline = time.monotonic() + CONTRACT["max_seconds"]
    out = {"schema": CONTRACT["id"], "contract": deepcopy(CONTRACT), "state": "blocked_preflight",
        "paths": [], "total_return": None, "net_return": None, "mdd": None, "adoption": None,
        "strict_pit_eligible": False, "live_order_eligible": False, "registered_verdict": False,
        "feature_reproduction": {"state": "not_run"},
        "code_sha256": {"diagnostic": digest(Path(__file__))}}
    try:
        out["input_sha256"] = {name: digest(root / name) for name in EXPECTED}
        model, result, cfg, hashes = checked_inputs(root)
        out["input_sha256"] = hashes
        if result["model"] != model or result["model_sha256"] != hashes[MODEL]:
            raise ValueError("embedded_model_mismatch")
        frozen = {"evaluation_start": "2024-10-02", "evaluation_end": "2024-12-30",
            "liquidate_through": "2025-01-31", "starting_cash": 10_000_000,
            "max_positions": 6, "phase_count": 5, "horizon_open_to_open_sessions": 5}
        if any(cfg[k] != v for k, v in frozen.items()):
            raise ValueError("frozen_comparison_mismatch")
        out["code_sha256"].update({"core_orders": digest(Path(core.__file__)),
            "execution_book": digest(Path(__import__(ExecutionBook.__module__, fromlist=["__file__"]).__file__))})
        for name, sha in EXECUTION_CODE.items():
            if digest(root / name) != sha:
                raise ValueError("execution_code_mismatch:" + name)
        out["saved_row_reconciliation"] = reconcile_saved_rows(result, cfg)
        out["baseline_saved_fills"] = saved_fill_baseline(result["scenarios"]["75bp_full"]["ridge"])
        reproduction, by_day, panel = reproduce_fixed_predictions(root, model, result, cfg, deadline=deadline)
        out["feature_reproduction"] = reproduction
        reviewed = checked_reviewed(root, result, by_day, panel)
        out["reviewed_facts"] = result["reviewed_share_changes"]
        old = restore_old_intents(result, cfg, by_day, panel, reviewed, deadline=deadline)
        out["old_intent_reconciliation"] = {"state": "matched", "phases": old,
            "scope": "original actions/rank/budget/horizon/valid_until and close equity; no old quantity prerequisite"}
        out["state"] = "execution_in_progress"
        for phase in range(5):
            out["active_phase"] = phase
            out["paths"].append(replay_pair(phase, result, cfg, by_day, panel, reviewed, old[phase], deadline=deadline))
        del out["active_phase"]
        out["state"] = "partial_source_stop" if any(p["stop"] for p in out["paths"]) else "price_only_complete"
        out["reason"] = "raw_price_execution_only_rights_unverified"
        out["new_execution_paths"] = 2*len(out["paths"])
        out["cost_hypothesis"] = ("supported_in_comparable_prefix" if all(
            p["comparable_prefix_difference"] and p["comparable_prefix_difference"]["cost_saved_krw"] > 0
            for p in out["paths"]) else "not_supported_in_all_phases")
        out["strategy_improvement"] = None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        out["reason"] = str(exc)
        if out["state"] == "execution_in_progress":
            out["state"] = "rejected_execution"
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite a diagnostic")
    result = diagnose(args.repo.resolve())
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({"state": result["state"], "reason": result["reason"], "paths": len(result["paths"])}))
    return 2 if result["state"] in ("blocked_preflight", "rejected_execution") else 0


if __name__ == "__main__":
    raise SystemExit(main())
