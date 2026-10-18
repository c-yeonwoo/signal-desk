"""Offline calendar/account replay. Source admission belongs to the audit CLI.

Daily opens are proxies, not historical auction-liquidity observations. Account
completion is not statistical adoption; no production engine imports this file.
"""

import datetime as dt

from signal_desk import market_clock
from signal_desk.signals import research_action_clocks as clocks
from signal_desk.signals.research_net_alpha_account import AccountLedger
from signal_desk.signals.research_net_alpha_execution import ExecutionBook, portfolio_plan
from signal_desk.signals.research_net_alpha_v2 import LABEL_KINDS
from signal_desk.signals.research_net_alpha_v2 import number


def sessions(start, end):
    return [s.date().isoformat() for s in market_clock._calendar("kr").sessions_in_range(start, end)
            if market_clock.is_session("kr", s.date().isoformat())]


def schedule(spec, phase, scenario):
    if type(phase) is not int or not 0 <= phase < spec["phase_count"] or scenario not in spec["scenarios"]:
        raise ValueError("path outside frozen contract")
    decisions = sessions(spec["evaluation_start"], spec["evaluation_end"])[phase::spec["rebalance_sessions"]]
    if not decisions:
        raise ValueError("phase has no scheduled decision")
    # Last decision +6 open; delay scenario retains its extra submission session.
    terminal = market_clock.next_sessions("kr", decisions[-1], 6)[-1]
    submit_on = market_clock.previous_session("kr", terminal)
    due = market_clock.next_sessions("kr", submit_on, 2 if scenario.endswith("delay1") else 1)[-1]
    if due > spec["liquidation_through"]:
        raise ValueError("terminal open outside fixed tail")
    return {"decisions": decisions, "terminal_submit": submit_on, "terminal_open": due}


def run_path(spec, scenario, arm, phase, *, snapshots, bars, identities, evidence,
             model, bias, input_ids):
    """Independent account using admitted as-of inputs, never outcome labels.

    bars[day] contains opens, closes and volumes. Opens require source_id and
    explicit tradability, closes must omit stale/nontrading reference marks.
    A gap is retained even if abstention happens to avoid a subsequent loss.
    """
    if arm not in spec["arms"]:
        raise ValueError("unknown policy arm")
    clock = schedule(spec, phase, scenario)
    dates = sessions(spec["evaluation_start"], spec["liquidation_through"])
    if set(dates)-set(bars) or set(dates)-set(identities):
        raise ValueError("replay market calendar or identity inventory incomplete")
    book = ExecutionBook(spec, scenario)
    ledger = AccountLedger(book, evidence, identities, f"{scenario}/{arm}/{phase}")
    frames, decisions, gaps = [], [], []
    for day in dates:
        row = bars[day]
        ledger.advance(day, "before_open")
        opens = ledger.gate_opens(day, row["opens"])
        for order in book.pending:
            if order["due_on"] <= day:
                security = identities[day].get(order["ticker"])
                if (security is None or evidence.coverage(security, day, day, LABEL_KINDS) is None
                        or evidence.halted(security, clocks.session_time(day, "open"), clocks.session_time(day, "open")) is None):
                    gaps.append({"on":day, "ticker":order["ticker"], "reason":"execution_scope_identity_or_halt_unverified"})
                observation = row["opens"].get(order["ticker"], {})
                if not observation.get("source_id") or number(observation.get("price")) is None:
                    gaps.append({"on":day, "ticker":order["ticker"], "reason":"opening_observation_missing"})
        open_prices = {t:o["price"] for t,o in opens.items() if o.get("open_tradable") is True}
        opening_nav = ledger.valuation(day, open_prices, clocks.session_time(day, "open"))["value"]
        if opening_nav is None:
            ledger.cancel_orders(None, day, "buys_frozen_for_unvalued_account", buys_only=True)
        extra = max(0, opening_nav-book.price_nav(open_prices)) if opening_nav is not None else 0
        book.open_session(day, opens, noncash_nav=extra)
        ledger.record(day, clocks.session_time(day, "open"))
        ledger.advance(day, "close")
        close_at = clocks.session_time(day)
        mark = ledger.valuation(day, row["closes"], close_at)
        if day == clock["terminal_submit"]:
            ledger.cancel_orders(None, day, "cancelled_at_terminal_decision")
            book.submit_exits(day, list(book.holdings), row["closes"], row["volumes"],
                              input_id=input_ids[day])
            decisions.append({"on": day, "state": "terminal_submission", "input_id": input_ids[day]})
        elif day in clock["decisions"]:
            snapshot = snapshots.get(day)
            reason = None
            if snapshot is None or snapshot["date"] != day:
                reason = "scheduled_snapshot_missing"
            elif snapshot["state"] == "input_gap":
                reason = "scheduled_input_gap"
            elif snapshot["state"] == "no_new_decision_insufficient_universe":
                # Verified insufficient U is a declared no-decision, not a data gap.
                reason = "verified_insufficient_universe"
            elif snapshot["state"] != "ready":
                raise ValueError("unknown snapshot admission state")
            elif mark["value"] is None or mark["value"] <= 0 or len(book.holdings) > spec["max_positions"]:
                reason = "account_value_or_assets_unavailable"
            else:
                previous = {t:q*row["closes"][t]/mark["value"] for t,q in book.holdings.items()}
                try:
                    plan = portfolio_plan(snapshot, model, bias, previous, row["caps"], spec, arm=arm)
                except ValueError as exc:
                    if str(exc) != "held risk/unit history missing; defer optimization":
                        raise
                    reason = "held_risk_history_unavailable"
                else:
                    book.submit(day, plan["weights"], row["closes"], row["volumes"], mark["value"],
                        priority=plan["priority"], input_id=input_ids[day], budget=plan["budget"],
                        force_roundtrip=plan["force_roundtrip"])
            if reason:
                ledger.cancel_orders(None, day, "buys_frozen_for_deferred_decision", buys_only=True)
                if reason != "verified_insufficient_universe":
                    gaps.append({"on": day, "reason": reason})
                # Only independently established exits. A missing row/price is
                # not an instruction to turn a holding into cash.
                if snapshot and snapshot["date"] == day and len(snapshot.get("rows", [])) == spec["universe_count"]:
                    states = {r["ticker"]:r["state"] for r in snapshot["rows"]}
                    exiting = [t for t in book.holdings if t not in states or states[t] == "excluded"]
                    already = {o["ticker"] for o in book.pending if o["side"] == "sell"}
                    safe = ledger.gate_opens(day, {t:{"price":p, "open_tradable":True,
                        "source_id":input_ids[day]} for t,p in row["closes"].items()}, at=close_at)
                    exiting = [t for t in exiting if t not in already and safe.get(t, {}).get("open_tradable")]
                    book.submit_exits(day, exiting, row["closes"], row["volumes"], input_id=input_ids[day])
            decisions.append({"on": day, "state": reason or "submitted", "input_id": input_ids[day]})
        ledger.advance(day, "after_close")
        frames.append(ledger.frame(day, row["closes"], close_at+dt.timedelta(microseconds=1)))
    final_at = clocks.timestamp(spec["amendment"]["labels"]["evaluation_outcome_fact_cutoff"])
    marks = [{"on":f["on"], **ledger.mark(f, final_at=final_at)} for f in frames]
    reasons = {r for m in marks for r in m["reasons"]}
    if any(m["value"] is None or m["value"] <= 0 for m in marks):
        reasons.add("intermediate_nav_unverified")
    if book.holdings or book.pending:
        reasons.add("terminal_assets_or_orders_unliquidated")
    if gaps:
        reasons.add("scheduled_input_or_account_gap")
    if ledger.gaps:
        reasons.add("account_action_gap")
    complete = not reasons
    values = [float(spec["initial_cash_krw"])]+[m["value"] for m in marks]
    mdd, peak = 0.0, values[0]
    if complete:
        for value in values[1:]:
            peak = max(peak, value)
            mdd = min(mdd, value/peak-1)
    return {"scenario":scenario, "arm":arm, "phase":phase, "schedule":clock,
        "accounting_complete":complete, "reasons":sorted(reasons), "gaps":gaps,
        "account_gaps":ledger.gaps, "journal":ledger.journal, "decisions":decisions,
        "marks":marks, "execution":book.state(),
        "net_return":values[-1]/values[0]-1 if complete else None,
        "mdd":mdd if complete else None,
        "turnover":sum(f["shares"]*f["price"] for f in book.fills)/values[0],
        "strict_pit_eligible":False, "live_order_eligible":False,
        "adoption":"not_evaluated_statistical_and_attribution_gates_pending",
        "open_execution_basis":"regular_session_daily_open_proxy"}
