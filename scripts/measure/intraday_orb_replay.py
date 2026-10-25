"""Frozen opening-range comparison from preserved SIP sources. Never submits orders.

Historical source reconstruction is distinct from live delivery and a PIT universe.
Missing executions interrupt the account; they are never zero-return trades.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean, median, pstdev

from signal_desk import market_clock
from signal_desk.broker import execution
from signal_desk.ingest import alpaca_research

SYMBOLS = ("AAPL", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "XOM")
POLICY = "largecap-orb-rvol-v1-panel-reconstruction"
SECOND = 1_000_000_000
MINUTE = 60 * SECOND


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()


def write_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(json_bytes(value))


def clock(value: str) -> int:
    return alpaca_research.clock_ns(value)


def stamp(value: int) -> str:
    if value % SECOND:
        raise ValueError("source requests require whole seconds")
    return dt.datetime.fromtimestamp(value // SECOND, dt.timezone.utc).isoformat()


def session_clocks(day: str) -> tuple[int, int]:
    instant = dt.datetime.fromisoformat(day + "T17:00:00+00:00")
    window = market_clock.regular_window("us", instant)
    if window is None:
        raise ValueError("not an exchange session")
    return tuple(clock(t.isoformat()) for t in window)


def plan() -> dict:
    cal = market_clock._calendar("us")
    warmup = [s.date().isoformat() for s in cal.sessions_in_range("2023-12-01", "2023-12-29")]
    evaluation = [s.date().isoformat() for s in cal.sessions_in_range("2024-01-02", "2024-12-31")][:240]
    if len(warmup) != 20 or len(evaluation) != 240:
        raise ValueError("frozen session counts differ")
    return {"schema": "intraday-orb-source-plan-v1", "market": "us", "symbols": list(SYMBOLS),
            "sessions": warmup + evaluation, "evaluation_sessions": evaluation,
            "splits": {"development": evaluation[:120], "validation": evaluation[120:180],
                       "sealed_evaluation": evaluation[180:]},
            "policy": POLICY, "initial_nav_usd": 10000,
            "publication_lag_seconds_assumed": 1, "order_ttl_seconds": 5,
            "top_k": 2, "warmup_same_clock_sessions": 14,
            "base_non_spread_cost_bps_each_side_assumed": 27,
            "stress_non_spread_cost_bps_each_side_assumed": 54,
            "size_units_per_share_conservative_assumed": 1,
            "source_attestations": {"pit_largecap_universe": False,
                                    "account_cost_vintage": False,
                                    "historical_quote_size_units": False,
                                    "security_events": False},
            "cohort_status": "fixed_survivor_panel_not_market_universe",
            "live_order_eligible": False}


def checked_source(path: Path, schema: str) -> tuple[dict, dict]:
    envelope = json.loads(path.read_bytes())
    if envelope.get("schema") != schema or envelope.get("status") not in ("observed", "empty"):
        raise ValueError("unobserved or incompatible source: " + str(path))
    raw = base64.b64decode(envelope["raw_b64"], validate=True)
    if (len(raw) != envelope["raw_bytes"]
            or hashlib.sha256(raw).hexdigest() != envelope["raw_sha256"]):
        raise ValueError("source digest mismatch")
    body = json.loads(raw)
    if body.get("symbol") != envelope.get("symbol"):
        raise ValueError("source identity mismatch")
    return envelope, body


def load_bars(directory: Path) -> dict:
    sources = {}
    for path in sorted((directory / "bars").rglob("*.json")):
        env, body = checked_source(path, "alpaca-historical-bar-window-v1")
        request = env["request"]
        bounds = env["window"]
        expected = alpaca_research.bar_parameters(env["symbol"], bounds["start"], bounds["end"])
        if request != expected or body.get("next_page_token") or env.get("has_next_page"):
            raise ValueError("partial or incompatible bar request")
        symbol, day = env["symbol"], request["asof"]
        opening, closing = session_clocks(day)
        if clock(bounds["start"]) != opening or clock(bounds["end"]) != closing:
            raise ValueError("full scheduled regular session required")
        rows, previous = [], None
        for row in body.get("bars") or []:
            t = clock(row["t"])
            values = [row.get(k) for k in ("o", "h", "l", "c", "v", "vw")]
            if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in values) or min(values[:4]) <= 0 or values[4] < 0
                    or int(values[4]) != values[4] or values[5] <= 0
                    or not row["l"] <= min(row["o"], row["c"]) <= max(row["o"], row["c"]) <= row["h"]
                    or not opening <= t < closing or t % MINUTE
                    or (previous is not None and t <= previous)):
                raise ValueError("invalid source bar")
            rows.append({"t": t, **{k: row[k] for k in ("o", "h", "l", "c", "v", "vw")}})
            previous = t
        expected_clocks = list(range(opening, closing, MINUTE))
        if [row["t"] for row in rows] != expected_clocks:
            raise ValueError("missing minute bars: " + symbol + ":" + day)
        key = symbol, day
        if key in sources:
            raise ValueError("multiple source versions")
        sources[key] = {"bars": rows, "sha256": env["raw_sha256"]}
    return sources


def load_quotes(directory: Path) -> dict:
    sources = {}
    for path in sorted((directory / "quotes").rglob("*.json")):
        env, body = checked_source(path, "alpaca-historical-quote-window-v1")
        request = env["request"]
        expected = alpaca_research.parameters(env["symbol"], request["start"], request["end"])
        if request != expected:
            raise ValueError("incompatible quote request")
        key = env["symbol"], request["start"], request["end"]
        if key in sources and sources[key]["raw_sha256"] != env["raw_sha256"]:
            raise ValueError("conflicting source versions")
        rows = body.get("quotes") or []
        if (not isinstance(rows, list) or len(rows) != env.get("quote_count")
                or env.get("has_next_page") is not bool(body.get("next_page_token"))
                or env["status"] != ("observed" if rows else "empty")):
            raise ValueError("quote source pagination/schema mismatch")
        times = [clock(row["t"]) for row in rows]
        if times != sorted(times) or any(not clock(key[1]) <= t <= clock(key[2]) for t in times):
            raise ValueError("quote source clock mismatch")
        sources[key] = {**env, "rows": rows}
    return sources


def opening_candidates(sources: dict, sessions: list[str], day: str, ranking: str) -> list[dict]:
    if ranking not in ("rvol", "turnover"):
        raise ValueError("unknown frozen comparator")
    index = sessions.index(day)
    if index < 20:
        raise ValueError("20 prior source sessions required")
    opening, closing = session_clocks(day)
    candidates = []
    for symbol in SYMBOLS:
        today = sources[(symbol, day)]["bars"]
        history = [sources[(symbol, prior)]["bars"] for prior in sessions[index - 20:index]]
        first = today[:5]
        if [r["t"] for r in first] != list(range(opening, opening + 5 * MINUTE, MINUTE)):
            raise ValueError("opening window incomplete")
        prior_volumes = [sum(row["v"] for row in bars[:5]) for bars in history[-14:]]
        if not all(v > 0 for v in prior_volumes):
            raise ValueError("opening volume denominator unknown")
        op, hi, lo, close = first[0]["o"], max(r["h"] for r in first), min(r["l"] for r in first), first[-1]["c"]
        previous_close = history[-1][-1]["c"]
        rvol = sum(row["v"] for row in first) / mean(prior_volumes)
        if previous_close < 10 or abs(op / previous_close - 1) > .05 or (hi - lo) / op > .02 or hi == lo or rvol < 1:
            continue
        candidates.append({"symbol": symbol, "day": day, "open": op, "high": hi, "low": lo,
                           "close": close, "rvol": rvol, "opening": opening, "closing": closing,
                           "turnover": median(sum(r["v"] * r["vw"] for r in bars) for bars in history),
                           "source_sha256": sources[(symbol, day)]["sha256"]})
    return sorted(candidates, key=lambda r: (-r[ranking], r["symbol"]))[:2]


def entry_intent(candidate: dict, bars: list[dict], delay_seconds: int = 0) -> dict | None:
    if delay_seconds not in (0, 60):
        raise ValueError("unregistered delay")
    if candidate["close"] <= candidate["open"]:
        return None  # The frozen shortlist is not refilled.
    prior_close = candidate["close"]
    for bar in bars[5:]:
        ready = bar["t"] + MINUTE + SECOND + delay_seconds * SECOND
        if ready >= candidate["opening"] + 60 * MINUTE:
            break
        if prior_close <= candidate["high"] < bar["c"]:
            return {**candidate, "ready": ready, "expires": ready + 5 * SECOND,
                    "limit": candidate["high"] + .25 * (candidate["high"] - candidate["low"]),
                    "initial_stop": candidate["low"]}
        prior_close = bar["c"]
    return None


def request_window(symbol: str, at: int) -> dict:
    return {"symbol": symbol, "start": stamp(at), "end": stamp(at + 5 * SECOND)}


def quote_at(index: dict, symbol: str, at: int) -> tuple[dict | None, dict | None]:
    request = request_window(symbol, at)
    source = index.get((symbol, request["start"], request["end"]))
    if source is None:
        return None, request
    for row in source["rows"]:
        t = clock(row["t"])
        if not at <= t < at + 5 * SECOND:
            continue
        values = [row.get(k) for k in ("bp", "ap", "bs", "as")]
        if (row.get("c") != ["R"] or not row.get("bx") or not row.get("ax")
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) and v > 0 for v in values)
                or row["bp"] >= row["ap"] or not all(float(v).is_integer() for v in values[2:])):
            continue
        return {**row, "ns": t, "source_sha256": source["raw_sha256"]}, None
    if source.get("has_next_page"):
        return None, request  # An unseen page cannot certify no fill.
    return None, None


def exit_clock(intent: dict, quote: dict, bars: list[dict]) -> dict:
    target = quote["ap"] + 2 * (quote["ap"] - intent["initial_stop"])
    deadline = min(quote["ns"] + 3600 * SECOND, intent["closing"] - 5 * MINUTE)
    ambiguity = []
    for bar in bars:
        end = bar["t"] + MINUTE
        if end <= quote["ns"] or end + SECOND > deadline:
            continue
        if bar["l"] <= intent["initial_stop"]:
            if bar["h"] >= target or bar["t"] < quote["ns"]:
                ambiguity.append("intrabar_stop_order_unknown")
            return {"at": end + SECOND, "reason": "stop_bar_close_reconstruction", "ambiguity": ambiguity}
        if bar["h"] >= target:
            if bar["t"] < quote["ns"]:
                ambiguity.append("entry_bar_target_ignored")
                continue
            return {"at": end + SECOND, "reason": "target_bar_close_reconstruction", "ambiguity": ambiguity}
    # Provider query bounds are whole seconds; round UP, never before first fill + 60m.
    at = ((deadline + SECOND - 1) // SECOND) * SECOND
    return {"at": at, "reason": "time_exit", "ambiguity": ambiguity,
            "deadline_ns": deadline, "query_round_up_ns": at - deadline}


def costs(bps: int) -> dict:
    if bps not in (27, 54):
        raise ValueError("unregistered cost scenario")
    return {"version": 1, "market": "us", "slippage_bps": 0, "commission_bps": bps,
            "sell_tax_bps": 0, "sec_sell_bps": 0, "finra_per_share": 0, "finra_cap": 0}


def quote_plan(sources: dict, quotes: dict, frozen: dict) -> dict:
    """Plan all missing entry/exit windows, without repeatedly replaying one blocked day."""
    windows = {}
    for day in frozen["evaluation_sessions"]:
        for ranking in ("rvol", "turnover"):
            for candidate in opening_candidates(sources, frozen["sessions"], day, ranking):
                for delay in (0, 60):
                    bars = sources[(candidate["symbol"], day)]["bars"]
                    intent = entry_intent(candidate, bars, delay)
                    if intent is None:
                        continue
                    entry, need = quote_at(quotes, intent["symbol"], intent["ready"])
                    if need:
                        windows[tuple(need.values())] = need
                    elif entry and entry["ap"] <= intent["limit"] and entry["ap"] > intent["initial_stop"]:
                        exit_plan = exit_clock(intent, entry, bars)
                        _, need = quote_at(quotes, intent["symbol"], exit_plan["at"])
                        if need:
                            windows[tuple(need.values())] = need
    return {"schema": "intraday-orb-quote-plan-v1", "market": "us",
            "windows": sorted(windows.values(), key=lambda r: (r["start"], r["symbol"])),
            "plan_sha256": hashlib.sha256(json_bytes(frozen)).hexdigest(), "live_order_eligible": False}


def replay(sources: dict, quotes: dict, frozen: dict, ranking: str, *, bps: int = 27,
           delay: int = 0, liquidity_fraction: float = 1, size_scale: int = 1) -> dict:
    if liquidity_fraction not in (1, .5):
        raise ValueError("unregistered liquidity scenario")
    if isinstance(size_scale, bool) or size_scale not in (1, 100):
        raise ValueError("unregistered historical size interpretation")
    nav, peak, mdd, days, trades, needs = float(frozen["initial_nav_usd"]), float(frozen["initial_nav_usd"]), 0., [], [], {}
    for day in frozen["evaluation_sessions"]:
        day_start, day_trades, day_complete = nav, [], True
        try:
            candidates = opening_candidates(sources, frozen["sessions"], day, ranking)
        except (KeyError, ValueError) as exc:
            return {"complete": False, "blocked_day": day, "reason": str(exc),
                    "ranking": ranking, "net_return_pct": None, "days": days,
                    "trades": trades, "needs": list(needs.values())}
        intents = [i for c in candidates if (i := entry_intent(c, sources[(c["symbol"], day)]["bars"], delay))]
        for intent in sorted(intents, key=lambda row: (row["ready"], row["symbol"])):
            realized_before = sum(t["pnl"] for t in day_trades if t["exit_ns"] <= intent["ready"])
            if realized_before <= -.0075 * day_start:
                continue
            entry, need = quote_at(quotes, intent["symbol"], intent["ready"])
            if need:
                needs[tuple(need.values())], day_complete = need, False
                continue
            if entry is None:
                continue
            spread = (entry["ap"] - entry["bp"]) / ((entry["ap"] + entry["bp"]) / 2) * 10000
            if entry["ap"] > intent["limit"] or entry["ap"] <= intent["initial_stop"] or spread > 10:
                continue
            unit_risk = intent["limit"] - intent["initial_stop"] + intent["limit"] * 2 * bps / 10000
            qty = math.floor(min(.0025 * day_start / unit_risk,
                                 .2 * day_start / (intent["limit"] * (1 + bps / 10000)),
                                 entry["as"] * size_scale * liquidity_fraction))
            if qty < 1:
                continue
            exit_plan = exit_clock(intent, entry, sources[(intent["symbol"], day)]["bars"])
            exit_quote, need = quote_at(quotes, intent["symbol"], exit_plan["at"])
            if need:
                needs[tuple(need.values())], day_complete = need, False
                continue
            if exit_quote is None or exit_quote["bs"] * size_scale * liquidity_fraction < qty:
                # A position already exists: missing liquidation is not a no-trade.
                day_complete = False
                continue
            bought = execution.calculate(entry["ap"], qty, "buy", "us", assumptions=costs(bps))
            sold = execution.calculate(exit_quote["bp"], qty, "sell", "us", assumptions=costs(bps))
            trade = {"symbol": intent["symbol"], "day": day, "quantity": qty,
                     "entry_ns": entry["ns"], "exit_ns": exit_quote["ns"],
                     "entry_ask": entry["ap"], "exit_bid": exit_quote["bp"],
                     "pnl": bought.cash_change + sold.cash_change,
                     "fees": bought.total_fees + sold.total_fees,
                     "entry_notional": bought.gross_notional, "initial_stop": intent["initial_stop"],
                     "planned_stop_loss": qty * unit_risk,
                     "entry_sha256": entry["source_sha256"], "exit_sha256": exit_quote["source_sha256"],
                     "exit_reason": exit_plan["reason"], "ambiguity": exit_plan["ambiguity"]}
            day_trades.append(trade)
        if not day_complete:
            return {"complete": False, "blocked_day": day, "reason": "missing_executable_quotes_or_liquidation",
                    "ranking": ranking, "net_return_pct": None, "days": days,
                    "trades": trades, "needs": list(needs.values())}
        nav += sum(t["pnl"] for t in day_trades)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
        opening, closing = session_clocks(day)
        exposure = sum(t["entry_notional"] * (t["exit_ns"] - t["entry_ns"]) / (closing - opening)
                       for t in day_trades) / day_start * 100
        days.append({"day": day, "nav": nav, "return_pct": (nav / day_start - 1) * 100,
                     "time_weighted_entry_notional_pct": exposure,
                     "trade_count": len(day_trades)})
        trades.extend(day_trades)
    return {"complete": True, "ranking": ranking, "non_spread_bps_each_side_assumed": bps,
            "delay_seconds": delay, "liquidity_fraction": liquidity_fraction,
            "quote_size_scale_assumed": size_scale,
            "net_return_pct": (nav / frozen["initial_nav_usd"] - 1) * 100,
            "daily_mdd_pct": mdd * 100,
            "mean_time_weighted_entry_notional_pct": mean(d["time_weighted_entry_notional_pct"] for d in days),
            "days": days, "trades": trades, "needs": []}


def compare(sources: dict, quotes: dict, frozen: dict, *, size_scale: int = 1) -> dict:
    if frozen != plan():
        raise ValueError("frozen source/policy plan differs")
    paths = {"rvol": replay(sources, quotes, frozen, "rvol", size_scale=size_scale),
             "turnover": replay(sources, quotes, frozen, "turnover", size_scale=size_scale),
             "double_cost": replay(sources, quotes, frozen, "rvol", bps=54, size_scale=size_scale),
             "delay_60s": replay(sources, quotes, frozen, "rvol", delay=60, size_scale=size_scale),
             "half_size": replay(sources, quotes, frozen, "rvol", liquidity_fraction=.5, size_scale=size_scale)}
    all_complete = all(p["complete"] for p in paths.values())
    missing = [key for key, verified in frozen["source_attestations"].items() if not verified]
    verdict = "not_adopted_source_incomplete" if missing else "not_adopted_requires_sealed_inference"
    checks, paired, reconstruction_verdict = {}, None, "incomplete"
    if not all_complete:
        verdict = "blocked_missing_inputs"
    else:
        for path in paths.values():
            path["splits"] = {name: split_metrics(path, days) for name, days in frozen["splits"].items()}
        chosen, baseline = (paths[key]["splits"]["sealed_evaluation"] for key in ("rvol", "turnover"))
        sample_days = set(frozen["splits"]["sealed_evaluation"])
        differences = [a["return_pct"] - b["return_pct"] for a, b in zip(paths["rvol"]["days"], paths["turnover"]["days"])
                       if a["day"] in sample_days]
        paired = paired_block_lower(differences)
        selected_trades = [t for t in paths["rvol"]["trades"] if t["day"] in sample_days]
        by_name = {name: sum(t["pnl"] for t in selected_trades if t["symbol"] == name) for name in SYMBOLS}
        by_day = {day: sum(t["pnl"] for t in selected_trades if t["day"] == day) for day in sample_days}
        profit = sum(by_day.values())
        checks = {"sealed_positive": chosen["return_pct"] > 0,
                  "sealed_beats_turnover": chosen["return_pct"] > baseline["return_pct"],
                  "paired_lower_above_zero": paired is not None and paired > 0,
                  "no_higher_daily_risk": chosen["mdd_pct"] >= baseline["mdd_pct"]
                      and chosen["daily_sd_pct"] <= baseline["daily_sd_pct"],
                  "stress_positive": all(paths[key]["splits"]["sealed_evaluation"]["return_pct"] > 0
                                         for key in ("double_cost", "delay_60s", "half_size")),
                  "not_single_name_dependent": profit - max(by_name.values(), default=0) > 0,
                  "not_single_day_dependent": profit - max(by_day.values(), default=0) > 0,
                  "no_intrabar_ambiguity": not any(t["ambiguity"] for t in selected_trades)}
        reconstruction_verdict = "passed_frozen_checks" if all(checks.values()) else "failed_frozen_checks"
        if not missing and not all(checks.values()):
            verdict = "rejected_in_reconstruction"
    return {"schema": "intraday-orb-reconstruction-comparison-v1", "policy": POLICY,
            "quote_size_scale_assumed": size_scale,
            "interpretation": "primary_conservative" if size_scale == 1 else "round_lot_sensitivity_only",
            "plan_sha256": hashlib.sha256(json_bytes(frozen)).hexdigest(),
            "paths": paths, "cash_control_return_pct": 0, "verdict": verdict,
            "conditional_reconstruction_verdict": reconstruction_verdict,
            "sealed_checks": checks, "paired_daily_excess_lower_97_5_pct": paired,
            "source_blockers": missing, "live_order_eligible": False,
            "limits": ["fixed_survivor_panel", "assumed_delivery_latency_and_costs",
                       "minute_exit_reconstruction_not_tick_stop", "unverified_historical_size_units",
                       "no_automatic_adoption"]}


def split_metrics(path: dict, sessions: list[str]) -> dict:
    rows = [r for r in path["days"] if r["day"] in set(sessions)]
    if [r["day"] for r in rows] != sessions:
        raise ValueError("split dates differ")
    equity, peak, mdd = 1., 1., 0.
    for row in rows:
        equity *= 1 + row["return_pct"] / 100
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    return {"return_pct": (equity - 1) * 100, "mdd_pct": mdd * 100,
            "daily_sd_pct": pstdev(r["return_pct"] for r in rows), "sessions": len(rows),
            "trade_count": sum(r["trade_count"] for r in rows),
            "mean_time_weighted_entry_notional_pct": mean(r["time_weighted_entry_notional_pct"] for r in rows)}


def paired_block_lower(differences: list[float]) -> float | None:
    if len(differences) != 60:
        return None
    rng = random.Random(20261025)
    means = []
    for _ in range(10000):
        sample = []
        for _ in range(12):
            start = rng.randrange(60)
            sample.extend(differences[(start + j) % 60] for j in range(5))
        means.append(mean(sample))
    return sorted(means)[249]


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("freeze", "needs", "compare"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--size-scale", type=int, choices=(1, 100), default=1,
                        help="100 is a separately reported historical round-lot sensitivity, not an adoption path")
    args = parser.parse_args()
    if args.command == "freeze":
        write_new(args.plan, plan())
        print(json.dumps({"plan": str(args.plan), "sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest()}))
        return
    frozen = json.loads(args.plan.read_bytes())
    if frozen != plan() or args.sources is None or args.output is None:
        parser.error("exact frozen plan, source directory and new output path required")
    if args.command == "needs":
        report = quote_plan(load_bars(args.sources), load_quotes(args.sources), frozen)
    else:
        report = compare(load_bars(args.sources), load_quotes(args.sources), frozen, size_scale=args.size_scale)
    write_new(args.output, report)
    print(json.dumps({"output": str(args.output), "verdict": report.get("verdict"),
                      "quote_windows": len(report.get("windows", []))}))


if __name__ == "__main__":
    main()
