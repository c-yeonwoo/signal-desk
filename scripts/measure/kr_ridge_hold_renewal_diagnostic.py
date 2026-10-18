"""Controller-only frozen hold/renewal price diagnostic; never a live strategy.

Only main() reads preserved market inputs. Importing this module is inert.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import datetime as dt
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

CONTRACT_PATH = "docs/KR-R3-HOLD-RENEWAL-CONTRACT-2026-10-18.md"
CONTRACT_COMMIT = "1667e997510644e1fbcce65cd03ad8119ee33031"
CONTRACT_SHA = "13ce5fbdfec1f41583e8ced2fd2d4d6c9f55db994bda3619ee888d2a152884bd"
ARMS = ("forced", "net", "candidate")
INITIAL = 10_000_000


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Budget:
    def __init__(self, seconds=600):
        self.wall = time.monotonic()
        self.cpu = time.process_time()
        self.seconds = seconds

    def elapsed(self):
        return {"wall": time.monotonic()-self.wall, "cpu": time.process_time()-self.cpu}

    def check(self):
        if max(self.elapsed().values()) >= self.seconds:
            raise TimeoutError("600_second_cpu_or_wall_limit")


def dependencies(root):
    # A staged sibling test must use the declared repo's existing kernel only.
    sys.path[:0] = [str(root), str(root / "src")]
    names = {
        "old": "scripts.measure.kr_ridge_turnover_diagnostic",
        "lab": "scripts.measure.kr_ridge_selection_lab",
        "ridge": "signal_desk.signals.research_ridge",
        "clock": "signal_desk.market_clock",
        "execution": "signal_desk.signals.research_net_alpha_execution",
        "panel": "scripts.measure.price_challenger_lab",
    }
    modules = {k: importlib.import_module(v) for k, v in names.items()}
    for module in modules.values():
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise ValueError("dependency_outside_declared_repo")
    return modules


def checked_contract(root, commit, budget):
    raw = (root / CONTRACT_PATH).read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONTRACT_SHA or commit != CONTRACT_COMMIT:
        raise ValueError("precommitted_contract_hash_or_commit_differs")
    committed = subprocess.check_output(
        ["git", "show", f"{commit}:{CONTRACT_PATH}"], cwd=root,
        timeout=max(.1, budget.seconds-max(budget.elapsed().values())))
    if raw != committed:
        raise ValueError("contract_not_equal_to_precommit")
    blocks = re.findall(rb"```json\s*\n(.*?)\n```", raw, re.S)
    if len(blocks) != 1:
        raise ValueError("contract_requires_unique_JSON")
    contract = json.loads(blocks[0])
    manifest = contract["controllerVerifiedInputSHA256"]
    if len(manifest) != 35:
        raise ValueError("manifest_not_35")
    hashes = {}
    for name, expected in manifest.items():
        budget.check()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or digest(path) != expected:
            raise ValueError("manifest_mismatch:" + name)
        hashes[name] = expected
    return contract, hashes


@dataclass
class CandidateState:
    ever_held: set = field(default_factory=set)
    last_flat_session: dict = field(default_factory=dict)
    exit_pending: set = field(default_factory=set)

    def fills(self, fills, before, session_index):
        quantities = dict(before)
        for fill in fills:
            ticker, quantity = fill["ticker"], fill["shares"]
            old = quantities.get(ticker, 0)
            new = old + (quantity if fill["side"] == "buy" else -quantity)
            if new < 0:
                raise ValueError("negative_fill_quantity")
            if fill["side"] == "buy":
                self.ever_held.add(ticker)
            if old > 0 and new == 0:
                self.last_flat_session[ticker] = session_index
                self.exit_pending.discard(ticker)
            quantities[ticker] = new

    def targets(self, selected, holdings, trends, session_index):
        target = {}
        for ticker in sorted(set(selected) | set(holdings)):
            held = holdings.get(ticker, 0) > 0
            if ticker not in self.ever_held and not held:
                allowed = ticker in selected
            elif held:
                allowed = (ticker not in self.exit_pending and ticker in selected
                           and trends[ticker]["passes"])
                if not allowed:
                    self.exit_pending.add(ticker)
            else:
                allowed = (ticker in selected and trends[ticker]["passes"] and
                           session_index-self.last_flat_session[ticker] >= 5)
            if allowed:
                target[ticker] = 1/6
        return target

    def trace(self):
        return {"ever_held": sorted(self.ever_held),
                "last_flat_session": dict(self.last_flat_session),
                "exit_pending": sorted(self.exit_pending)}


def positive(value):
    return isinstance(value, (float, int)) and math.isfinite(value) and value > 0


def number(value):
    try:
        parsed = float(str(value).replace(",", ""))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


class SourceStop(Exception):
    def __init__(self, day, ticker, field, stage, evidence_session=None):
        self.detail = {"session": day, "ticker": ticker, "field": field,
                       "stage": stage, "source_id": f"preserved-raw:{day}:{ticker}",
                       "reason": f"source_gap:{day}:{ticker}:{field}"}
        if evidence_session is not None:
            self.detail["evidence_session"] = evidence_session
            self.detail["source_id"] = f"preserved-raw:{evidence_session}:{ticker}"
        super().__init__(self.detail["reason"])


def open_marks(day, names, panel):
    marks = {}
    for ticker in sorted(names):
        opened = panel.bars.get((day, ticker), (None, None))[0]
        if not positive(opened):
            raise SourceStop(day, ticker, "open_or_halt", "open")
        marks[ticker] = {"price": opened, "open_tradable": True,
                         "source_id": f"preserved-raw:{day}:{ticker}"}
    return marks


def unit_check(day, ticker, by_day, panel, reviewed):
    i = panel.index[day]
    if i == 0:
        raise SourceStop(day, ticker, "previous_unit_missing", "close")
    previous = panel.sessions[i-1]
    before = number(by_day.get(previous, {}).get(ticker, {}).get("LIST_SHRS"))
    after = number(by_day.get(day, {}).get(ticker, {}).get("LIST_SHRS"))
    if not positive(before) or not positive(after):
        raise SourceStop(day, ticker, "LIST_SHRS_unit_unknown", "close")
    if before != after:
        fact = reviewed.get((day, ticker))
        if (fact is None or fact.holder_quantity_effect != "unchanged" or
                fact.listed_shares_before != before or fact.listed_shares_after != after):
            raise SourceStop(day, ticker, "unreviewed_unit_or_rights_change", "close")


def close_inputs(day, names, panel, by_day, reviewed, *, need_volume=False):
    closes, volumes = {}, {}
    for ticker in sorted(names):
        closed = panel.bars.get((day, ticker), (None, None))[1]
        if not positive(closed):
            raise SourceStop(day, ticker, "close", "close")
        unit_check(day, ticker, by_day, panel, reviewed)
        closes[ticker] = closed
        if need_volume:
            volume = number(by_day.get(day, {}).get(ticker, {}).get("ACC_TRDVOL"))
            if volume is None or volume < 0:
                raise SourceStop(day, ticker, "decision_volume", "close")
            volumes[ticker] = volume
    return closes, volumes


def trend_at(day, ticker, panel, by_day, reviewed):
    i = panel.index[day]
    history = panel.sessions[max(0, i-19):i+1]
    if len(history) != 20:
        raise SourceStop(day, ticker, "MA20_history", "close")
    values = []
    for session in history:
        closed = panel.bars.get((session, ticker), (None, None))[1]
        if not positive(closed):
            raise SourceStop(day, ticker, "MA20_close", "close", session)
        try:
            if session != history[0]:
                unit_check(session, ticker, by_day, panel, reviewed)
            elif not positive(number(by_day.get(session, {}).get(ticker, {}).get("LIST_SHRS"))):
                raise SourceStop(session, ticker, "LIST_SHRS_unit_unknown", "close")
        except SourceStop as exc:
            raise SourceStop(day, ticker, exc.detail["field"], "close", session) from exc
        values.append(closed)
    average = math.fsum(values)/20
    return {"passes": values[-1] >= average, "close": values[-1], "ma20": average,
            "history": [{"session": s, "close": v, "source_id": f"preserved-raw:{s}:{ticker}"}
                        for s, v in zip(history, values)]}


class FIFO:
    def __init__(self):
        self.lots = []

    def fill(self, fill, index):
        ticker, quantity = fill["ticker"], fill["shares"]
        if fill["side"] == "buy":
            self.lots.append({"ticker": ticker, "entry": fill["on"], "entry_index": index,
                "quantity": quantity, "remaining": quantity, "price": fill["price"],
                "buy_fee": fill["fee"], "realized": 0., "sell_principal": 0.,
                "sell_fee": 0., "exit_index": None})
            return
        remaining = quantity
        for lot in self.lots:
            if lot["ticker"] != ticker or not lot["remaining"]:
                continue
            sold = min(lot["remaining"], remaining)
            fee = fill["fee"]*sold/quantity
            lot["realized"] += sold*(fill["price"]-lot["price"])-fee-lot["buy_fee"]*sold/lot["quantity"]
            lot["sell_principal"] += sold*fill["price"]
            lot["sell_fee"] += fee
            lot["remaining"] -= sold
            remaining -= sold
            if not lot["remaining"]:
                lot["exit_index"] = index
            if not remaining:
                break
        if remaining:
            raise ValueError("FIFO_sell_without_buy")

    def metrics(self, closes, index, holdings):
        quantities, tickers, months = defaultdict(int), defaultdict(float), defaultdict(float)
        for lot in self.lots:
            ticker, remaining = lot["ticker"], lot["remaining"]
            quantities[ticker] += remaining
            value = lot["realized"]
            if remaining:
                value += remaining*(closes[ticker]-lot["price"])-lot["buy_fee"]*remaining/lot["quantity"]
            tickers[ticker] += value
            months[lot["entry"][:7]] += value
        if {t: q for t, q in quantities.items() if q} != holdings:
            raise ValueError("FIFO_holding_reconciliation")
        denominator = sum(abs(v) for v in tickers.values())
        return {"completedFIFOBuyLots": sum(l["remaining"] == 0 for l in self.lots),
            "distinctBuyFillDates": len({l["entry"] for l in self.lots}),
            "distinctTickers": len({l["ticker"] for l in self.lots}),
            "pnl_by_ticker": dict(tickers), "pnl_by_entry_month": dict(months),
            "absoluteTickerPnLShare": max(map(abs, tickers.values()), default=0)/denominator if denominator else None,
            "positiveEntryMonths": sum(v > 0 for v in months.values()),
            "holding_sessions": [(l["exit_index"] if l["exit_index"] is not None else index)-l["entry_index"] for l in self.lots],
            "fifo_price_pnl": sum(tickers.values()), "lots": deepcopy(self.lots)}


def execution_spec(period, cost):
    return {"scenarios": [f"{cost}bp_full"], "initial_cash_krw": INITIAL,
            "evaluation_start": period["decisionStart"], "evaluation_end": period["decisionEnd"],
            "liquidation_through": period["tailThrough"], "max_positions": 6,
            "buy_limit_above_close": .01, "decision_one_way_cost": .006,
            "prior_volume_participation": .01, "sell_residual_attempts": 5}


def submit_three(books, decision, state, trends, index, *, terminal=False):
    selected = {t for t, _ in decision.selected}
    candidate = {} if terminal else state.targets(selected, books["candidate"].holdings, trends, index)
    priorities = {t: -rank for t, rank in decision.selected}
    targets = {a: ({} if terminal else candidate if a == "candidate" else {t: 1/6 for t in selected}) for a in ARMS}
    # Validate all three before changing any residual queue.
    for arm, book in books.items():
        importlib.import_module("signal_desk.signals.research_net_alpha").orders(
            targets[arm], book.holdings, dict(decision.closes), dict(decision.volumes), INITIAL, book.spec)
    if terminal:
        state.exit_pending.update(books["candidate"].holdings)
    orders = {a: b.submit(decision.day, targets[a], dict(decision.closes), dict(decision.volumes),
        INITIAL, priority=priorities, input_id=f"hold-renewal/{decision.day}", budget=1,
        force_roundtrip=a == "forced" and not terminal) for a, b in books.items()}
    return {"common": asdict(decision), "targets": targets, "orders": orders,
            "terminal": terminal, "decision_session_index": index,
            "cooldown_sessions": {t: index-f for t, f in state.last_flat_session.items()},
            "candidate_state": state.trace(), "trends": trends}


def assert_close(actual, expected, label):
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=.01):
        raise ValueError(label)


def price_mdd(curve):
    if not curve:
        return None
    peak, worst = INITIAL, 0.
    for nav in curve:
        peak = max(peak, nav)
        worst = min(worst, 100*(nav/peak-1))
    return worst


def final_metrics(book, fifo, closes, index, curve, reason=None):
    incomplete = bool(book.holdings or book.pending or book.settlements)
    metrics = {"afterCostPricePnL": None, "beforeCostPricePnL": None,
               "fees": None, "priceMDDPct": None, "complete": False,
               "reason": reason or ("incomplete_tail" if incomplete else None),
               "confirmedPrefixPriceMDDPct": price_mdd(curve),
               "certifiedTotalReturn": None, "certifiedTotalReturnMDD": None}
    if reason:
        return metrics
    stats = fifo.metrics(closes, index, book.holdings)
    value = book.price_nav(closes)
    assert_close(stats["fifo_price_pnl"], value-INITIAL, "FIFO_NAV_PnL")
    mdd = price_mdd(curve)
    metrics["marked_prefix"] = {**stats, "priceNAV": value, "fees": book.fees, "priceMDDPct": mdd}
    if not incomplete:
        metrics.update(stats)
        metrics.update({"complete": True, "afterCostPricePnL": value-INITIAL,
                        "beforeCostPricePnL": value-INITIAL+book.fees,
                        "fees": book.fees, "priceMDDPct": mdd,
                        "turnover": sum(f["shares"]*f["price"] for f in book.fills)/INITIAL})
    return metrics


def pair_deltas(metrics):
    result = {}
    for other in ("forced", "net"):
        c, o = metrics["candidate"], metrics[other]
        if not c["complete"] or not o["complete"]:
            result["candidate-"+other] = None
            continue
        delta = {key: c[key]-o[key] for key in ("afterCostPricePnL", "beforeCostPricePnL", "fees")}
        assert_close(delta["afterCostPricePnL"], delta["beforeCostPricePnL"]-delta["fees"], "pair_delta")
        result["candidate-"+other] = delta
    return result


def replay(period, cost, phase, selections, feature_gaps, by_day, panel, reviewed, modules, budget):
    old, clock = modules["old"], modules["clock"]
    scheduled = sorted(selections)[phase::5]
    terminal = clock.next_sessions("kr", scheduled[-1], 5)[-1]
    books = {a: modules["execution"].ExecutionBook(execution_spec(period, cost), f"{cost}bp_full") for a in ARMS}
    initial = {a: b.state() for a, b in books.items()}
    fifos, curves = {a: FIFO() for a in ARMS}, {a: [] for a in ARMS}
    state, trace, submissions = CandidateState(), [], []
    last_marks, last_nav = {}, {a: float(INITIAL) for a in ARMS}
    previous_closes, stop, last_index = {}, None, 0
    sessions = [s for s in panel.sessions if period["decisionStart"] <= s <= period["tailThrough"]]
    for day in sessions:
        index = panel.index[day]
        last_index = index
        if stop:
            trace.append({"session": day, "arms": {a: None for a in ARMS}, "deltas": None, "reason": stop["reason"]})
            continue
        try:
            budget.check()
            names = set().union(*(set(b.holdings) | {o["ticker"] for o in b.pending} for b in books.values()))
            opens = open_marks(day, names, panel)
            before = {a: dict(b.holdings) for a, b in books.items()}
            start_fees = {a: b.fees for a, b in books.items()}
            for a, b in books.items():
                fills = b.open_session(day, opens)
                if a == "candidate":
                    state.fills(fills, before[a], index)
                for fill in fills:
                    fifos[a].fill(fill, index)
                old.reconcile_book(b, initial[a])
                assert_close(b.settled_cash+sum(s["amount"] for s in b.settlements), b.buying_power, "T2_buying_power")
            picked = selections[day] if day in scheduled else []
            decision_day = day in scheduled or day == terminal
            names |= {t for t, _ in picked}
            closes, volumes = close_inputs(day, names, panel, by_day, reviewed, need_volume=decision_day)
            # Feature/MA unit issues are known at this close, never before open.
            if day in scheduled and feature_gaps.get(day):
                raise SourceStop(**feature_gaps[day])
            trends = {}
            if day in scheduled:
                for ticker in {t for t, _ in picked} & state.ever_held:
                    trends[ticker] = trend_at(day, ticker, panel, by_day, reviewed)
            row = {}
            for a, b in books.items():
                gap = sum(q*(opens[t]["price"]-previous_closes[t]) for t, q in before[a].items())
                intraday = sum(q*(closes[t]-opens[t]["price"]) for t, q in b.holdings.items())
                nav = b.price_nav(closes)
                assert_close(nav-last_nav[a], gap+intraday-(b.fees-start_fees[a]), "daily_gap_intraday_fees")
                stat = fifos[a].metrics(closes, index, b.holdings)
                assert_close(stat["fifo_price_pnl"], nav-INITIAL, "daily_FIFO_NAV")
                row[a] = {"settled_cash": b.settled_cash, "settlements": deepcopy(b.settlements),
                    "buying_power": b.buying_power, "holdings": dict(b.holdings), "priceNAV": nav,
                    "fees": b.fees, "gap": gap, "intraday": intraday,
                    "exposure": sum(q*closes[t] for t, q in b.holdings.items()),
                    "afterCostPricePnL": nav-INITIAL, "beforeCostPricePnL": nav-INITIAL+b.fees,
                    "complete": True}
                row[a]["exposure_ratio"] = row[a]["exposure"]/nav if nav else None
                curves[a].append(nav)
                last_nav[a] = nav
            if decision_day:
                decision = old.CommonDecision(day, INITIAL,
                    tuple((t, rank+1) for rank, (t, _) in enumerate(picked)),
                    tuple(sorted(closes.items())), tuple(sorted(volumes.items())))
                submissions.append(submit_three(books, decision, state, trends, index, terminal=day == terminal))
            trace.append({"session": day, "arms": row, "deltas": pair_deltas(row)})
            last_marks, previous_closes = closes, closes
        except SourceStop as exc:
            stop = exc.detail
        except TimeoutError as exc:
            stop = {"session": day, "stage": "budget", "reason": str(exc), "kind": "timeout"}
        except (ValueError, KeyError, TypeError) as exc:
            stop = {"session": day, "stage": "implementation", "reason": str(exc), "kind": "implementation_failure"}
        if stop:
            trace.append({"session": day, "arms": {a: None for a in ARMS}, "deltas": None, "reason": stop["reason"]})
    try:
        metrics = {a: final_metrics(b, fifos[a], last_marks, last_index, curves[a], stop["reason"] if stop else None) for a, b in books.items()}
    except (ValueError, KeyError, TypeError) as exc:
        stop = {"session": sessions[-1], "stage": "final_reconciliation", "reason": str(exc),
                "kind": "implementation_failure"}
        metrics = {a: final_metrics(b, fifos[a], last_marks, last_index, curves[a], stop["reason"]) for a, b in books.items()}
    return {"period": period["id"], "cost": cost, "phase": phase, "stop": stop,
            "terminal": terminal, "last_common_close": next((r["session"] for r in reversed(trace) if all(r["arms"].values())), None),
            "trace": trace, "submissions": submissions, "metrics": metrics, "deltas": pair_deltas(metrics),
            "confirmed_execution_prefix": {a: {**b.state(), "FIFO": deepcopy(fifos[a].lots),
                "buy_principal": sum(f["shares"]*f["price"] for f in b.fills if f["side"] == "buy"),
                "sell_principal": sum(f["shares"]*f["price"] for f in b.fills if f["side"] == "sell")} for a, b in books.items()}}


def sessions_between(clock, first, last):
    day, end = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    result = []
    while day <= end:
        if clock.is_session("kr", day):
            result.append(day.isoformat())
        day += dt.timedelta(days=1)
    return result


def feature_history_issue(day, prices, by_day, panel, reviewed, budget):
    """Check observed changes without imposing completeness on old metadata.

    The old price-eligible set feeds market20/ranking. Missing historical share
    metadata remains uncertified rather than overriding the frozen feature mask.
    """
    index = panel.index[day]
    history = panel.sessions[max(0, index-252):index+1]
    for ticker in prices:
        budget.check()
        for previous, current in zip(history, history[1:]):
            before = number(by_day.get(previous, {}).get(ticker, {}).get("LIST_SHRS"))
            after = number(by_day.get(current, {}).get(ticker, {}).get("LIST_SHRS"))
            if positive(before) and positive(after) and before != after:
                try:
                    unit_check(current, ticker, by_day, panel, reviewed)
                except SourceStop as exc:
                    return {"day": day, "ticker": ticker, "field": exc.detail["field"],
                            "stage": "close", "evidence_session": current}
    return None


def prepare(root, contract, modules, budget):
    lab, ridge, clock = modules["lab"], modules["ridge"], modules["clock"]
    for bound in (lab.FINANCIAL, lab.SCOPE):
        if not Path(bound).resolve().is_relative_to(root):
            raise ValueError("financial_binding_outside_repo")
    model = json.loads((root / contract["inputs"]["model"]["path"]).read_text())
    old_result = json.loads((root / modules["old"].RESULT).read_text())
    cfg = json.loads((root / contract["inputs"]["selectionPlan"]["path"]).read_text())
    modules["old"].reconcile_saved_rows(old_result, cfg)
    archives = [root / contract["inputs"]["priceArchivesUnder"] / n for n in contract["inputs"]["priceArchiveSHA256"]]
    days, sources = lab.load_krx_archives(archives)
    budget.check()
    panel = modules["panel"].PricePanel(days)
    by_day = {d: {r["ISU_CD"]: r for r in rows} for d, rows in days.items()}
    reviewed = modules["old"].checked_reviewed(root, old_result, by_day, panel)
    financials = lab.load_financials(lab.FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in lab.top200(days[contract["inputs"]["financial"]["rosterAsOf"]])}
    expected = {(r["date"], r["ticker"]): r["predicted_gross"] for r in old_result["predictions"]}
    selections, predictions, feature_gaps = {}, {}, {}
    for period in contract["periods"]:
        dates = sessions_between(clock, period["decisionStart"], period["decisionEnd"])
        if period.get("expectedSessions") and len(dates) != period["expectedSessions"]:
            raise ValueError("decision_calendar_count")
        needed_calendar = sessions_between(clock, period["decisionStart"], period["tailThrough"])
        if any(d not in by_day for d in needed_calendar):
            raise ValueError("missing_period_calendar_session")
        chosen, forecast_rows, gaps = {}, [], {}
        actual = {}
        for day in dates:
            budget.check()
            rows, prices, *_ = lab.features_at(day, panel, by_day, financials, roster)
            forecast = ridge.predict([r["features"] for r in rows], model["model"]) if rows else []
            ranked = sorted(((r["ticker"], float(p)) for r, p in zip(rows, forecast, strict=True)), key=lambda p: (-p[1], p[0]))
            if any(not math.isfinite(p) for _, p in ranked):
                raise ValueError("nonfinite_prediction")
            actual.update({(day, t): p for t, p in ranked})
            chosen[day] = [(t, p) for t, p in ranked[:6] if p > .0125] if len(rows) >= 160 else []
            forecast_rows.append({"date": day, "feature_names": len(rows), "ranked": ranked, "selected": chosen[day]})
            # No new missing-price gate for excluded top200 names. Check known
            # unit problems only in histories actually consumed by features.
            issue = feature_history_issue(day, prices, by_day, panel, reviewed, budget)
            if issue:
                gaps[day] = issue
        if period["id"] == "dev_2024q4":
            if actual.keys() != expected.keys() or any(not math.isclose(actual[k], expected[k], rel_tol=1e-12, abs_tol=1e-14) for k in expected):
                raise ValueError("Q4_all_prediction_rows_mismatch")
            for day in dates:
                if [list(p) for p in chosen[day]] != old_result["selections"][day]["selected"]["ridge"]:
                    raise ValueError("Q4_selection_rank_mismatch")
        selections[period["id"]], predictions[period["id"]], feature_gaps[period["id"]] = chosen, forecast_rows, gaps
    return selections, predictions, feature_gaps, by_day, panel, reviewed, sources


def gates(paths):
    failures, minimum, incomplete, invalid = [], [], [], []
    cells = {(p["cost"], p["phase"]) for p in paths if p["period"] == "historical_price_2025q1"}
    for cost in (75, 120):
        for phase in range(5):
            if (cost, phase) not in cells:
                incomplete.append(f"{cost}bp/phase{phase}/not_run")
    for path in paths:
        if path["period"] != "historical_price_2025q1":
            continue
        key = f"{path['cost']}bp/phase{path['phase']}"
        metrics = path.get("metrics", {})
        if (path.get("stop") or {}).get("kind") == "implementation_failure":
            invalid.append(key)
        for arm in ARMS:
            m = metrics.get(arm, {})
            if not m.get("complete"):
                incomplete.append(key+"/"+arm)
                continue
            for field, threshold in (("completedFIFOBuyLots", 12), ("distinctBuyFillDates", 6), ("distinctTickers", 6)):
                if m[field] < threshold:
                    minimum.append(key+"/"+arm+"/"+field)
        c = metrics.get("candidate", {})
        prefix_mdd = c.get("confirmedPrefixPriceMDDPct")
        if not c.get("complete") and prefix_mdd is not None and prefix_mdd < -10:
            failures.append(key+"/observed_prefix_price_MDD")
        if c.get("complete"):
            strict = path["cost"] == 75
            if (c["afterCostPricePnL"] <= 0 if strict else c["afterCostPricePnL"] < 0):
                failures.append(key+"/candidate_price_PnL")
            if c["priceMDDPct"] < -10:
                failures.append(key+"/price_MDD")
            if c["absoluteTickerPnLShare"] is None or c["absoluteTickerPnLShare"] > .5:
                failures.append(key+"/concentration")
            if c["positiveEntryMonths"] < 2:
                failures.append(key+"/entry_months")
            for arm in ("forced", "net"):
                other = metrics.get(arm, {})
                if other.get("complete"):
                    delta = c["afterCostPricePnL"]-other["afterCostPricePnL"]
                    if (delta <= 0 if strict else delta < 0):
                        failures.append(key+"/candidate-"+arm)
            if strict and metrics.get("forced", {}).get("complete") and c["fees"] >= metrics["forced"]["fees"]:
                failures.append(key+"/fees")
    return {"observedGateFailures": failures, "minimumNotMet": minimum, "sourceOrTimeoutIncomplete": incomplete,
            "implementationFailure": invalid, "limitedDevelopmentPriceSupport": not any((failures, minimum, incomplete, invalid)),
            "adoptionClaim": False, "historicalResearchAdoption": False, "livePromotion": False}


def diagnose(root, commit=CONTRACT_COMMIT):
    budget = Budget()
    # Preallocate every trading path, including unexecuted/null paths.
    paths = [{"period": p, "cost": c, "phase": phase,
              "metrics": {a: {"complete": False, "afterCostPricePnL": None,
                              "beforeCostPricePnL": None, "fees": None,
                              "priceMDDPct": None, "reason": "not_run"} for a in ARMS},
              "deltas": {"candidate-forced": None, "candidate-net": None},
              "stop": {"reason": "not_run"}} for p in ("dev_2024q4", "historical_price_2025q1")
             for c in (75, 120) for phase in range(5)]
    out = {"schema": "kr-r3-h5-ma20-renew-cool5-v1", "grade": "development_diagnostic",
           "state": "preflight", "paths": paths,
           "cash": [{"period": p, "series": None, "reason": "not_run"} for p in
                    ("dev_2024q4", "historical_price_2025q1")],
           "maximum_grid": 62, "trading_path_count": 60, "shared_cash_series_count": 2,
           "contract_commit": commit, "contract_sha256": CONTRACT_SHA,
           "code_sha256": {"runner": digest(__file__), "staged_tests": digest(
               Path(__file__).resolve().parents[2] / "tests/test_kr_ridge_hold_renewal_diagnostic.py")}, "actual_collection": 0,
           "fit_calls": 0, "pristineOOS": False, "certifiedTotalReturn": None}
    try:
        contract, hashes = checked_contract(root, commit, budget)
        out["input_sha256"] = hashes
        modules = dependencies(root)
        selections, predictions, gaps, by_day, panel, reviewed, sources = prepare(root, contract, modules, budget)
        out.update({"predictions": predictions, "sources": sources, "reviewed_facts": [
            {**asdict(f), "source_observed_at": f.source_observed_at.isoformat()} for f in reviewed.values()],
            "Q4_reproduction": "all_prediction_rows_selection_rank_matched"})
        out["state"] = "replay"
        for i, period in enumerate(contract["periods"]):
            out["cash"][i] = {"period": period["id"], "series": [{"session": d, "value": INITIAL} for d in
                sessions_between(modules["clock"], period["decisionStart"], period["tailThrough"])],
                "fees": 0, "interest_modeled": False, "independentSample": False}
        for i, path in enumerate(paths):
            budget.check()
            period = next(p for p in contract["periods"] if p["id"] == path["period"])
            paths[i] = replay(period, path["cost"], path["phase"], selections[path["period"]], gaps[path["period"]],
                              by_day, panel, reviewed, modules, budget)
            if paths[i]["stop"] and paths[i]["stop"].get("kind") in ("timeout", "implementation_failure"):
                raise TimeoutError(paths[i]["stop"]["reason"]) if paths[i]["stop"]["kind"] == "timeout" else ValueError(paths[i]["stop"]["reason"])
        out["state"] = "price_diagnostic_finished"
    except TimeoutError as exc:
        out.update(state="timeout", reason=str(exc))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        out.update(state="implementation_failure", reason=str(exc))
    finally:
        out["elapsed_seconds"] = budget.elapsed()
        for path in paths:
            if (path.get("stop") or {}).get("reason") == "not_run":
                path["stop"]["reason"] = out.get("reason", "not_run")
                for metrics in path["metrics"].values():
                    metrics["reason"] = path["stop"]["reason"]
        out["gates"] = gates(paths)
        out["limitations"] = "Raw-price/open proxies; dividends/rights not certified; no total-return or live promotion."
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract-commit", default=CONTRACT_COMMIT)
    args = parser.parse_args()
    # Reserve the new file before any actual history is processed.
    with args.output.open("x", encoding="utf-8") as stream:
        result = diagnose(args.repo.resolve(), args.contract_commit)
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({"state": result["state"], "elapsed_seconds": result["elapsed_seconds"]}))
    return 0 if result["state"] == "price_diagnostic_finished" else 2


if __name__ == "__main__":
    raise SystemExit(main())
