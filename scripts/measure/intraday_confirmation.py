"""One frozen follow-up: a breakout must survive one more completed minute.

2025 reconstruction, not a replay of the 2026 operating engine. No LLM or orders.
Uses the previous source validation, ask/bid ledger and account risk constraints.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess

from scripts.measure import intraday_orb_replay as orb
from scripts.measure.intraday_orb_sources import _save_new

POLICY = "largecap-orb-confirm-once-2025-v1"
NO_PRICE_EVIDENCE_SHA256 = "521622a388512d9b179109114be9e8d5d241b46a03f81c659a33dd3a62fdbb05"


def verified_absence(path: Path | None) -> set[tuple[str, str, int]]:
    """One reviewed SIP odd-lot-only minute, pinned before any 2025 P&L was opened.

    Alpaca emits no bar when all trades are ineligible for OHLC. This preserves
    emitted-bar turnover as a proxy, not exact dollar volume, without filling prices.
    """
    if path is None:
        return set()
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != NO_PRICE_EVIDENCE_SHA256:
        raise ValueError("unreviewed no-price evidence")
    env = json.loads(encoded)
    raw = base64.b64decode(env["raw_b64"], validate=True)
    request = {"symbols": "JPM", "start": "2025-02-07T17:55:00Z",
               "end": "2025-02-07T17:55:59.999999999Z", "feed": "sip",
               "asof": "2025-02-07", "limit": "10000", "sort": "asc"}
    body = json.loads(raw)
    rows = body.get("trades", {}).get("JPM") or []
    start = orb.clock(request["start"])
    if (env.get("schema") != "intraday-gap-trade-probe-v1" or env.get("http_status") != 200
            or env.get("request") != request or env.get("has_next_page") is not False
            or body.get("next_page_token") or env.get("raw_bytes") != len(raw)
            or env.get("raw_sha256") != hashlib.sha256(raw).hexdigest()
            or len(rows) != 95 or env.get("rows") != len(rows)
            or any(r.get("z") != "A" or "I" not in r.get("c", [])
                   or set(r["c"]) - {" ", "I", "4"}
                   or not start <= orb.clock(r["t"]) < start + orb.MINUTE for r in rows)):
        raise ValueError("invalid no-price evidence")
    return {("JPM", "2025-02-07", start)}


def plan() -> dict:
    cal = orb.market_clock._calendar("us")
    warmup = [s.date().isoformat() for s in cal.sessions_in_range("2024-11-01", "2024-12-31")][-20:]
    days = [s.date().isoformat() for s in cal.sessions_in_range("2025-01-02", "2025-06-30")][:120]
    if len(warmup) != 20 or len(days) != 120:
        raise ValueError("fixed session counts differ")
    return {"schema": "intraday-confirmation-source-plan-v1", "market": "us",
            "policy": POLICY, "symbols": list(orb.SYMBOLS), "sessions": warmup + days,
            "evaluation_sessions": days,
            "splits": {"development": days[:60], "sealed_evaluation": days[60:]},
            "initial_nav_usd": 10000, "confirmation_completed_bars": 1,
            "primary_non_spread_bps_each_side_assumed": 12,
            "stress_non_spread_bps_each_side_assumed": 24,
            "cost_basis": "10bp_commission_reference_plus_2bp_buffer_not_account_vintage_certification",
            "size_scale_assumed": 100, "quote_size_evidence": "2025_SIP_same_row_control_not_panel_certificate",
            "selection_and_risk": "same_as_2024_ORB_no_refill_no_reentry",
            "source_attestations": {"pit_largecap_universe": False, "account_cost_vintage": False,
                                    "historical_quote_size_units": False, "security_events": False},
            "existing_operating_engine_comparable": False, "live_order_eligible": False}


def confirmed_entry(candidate: dict, bars: list[dict], delay_seconds: int = 0) -> dict | None:
    """Only the first crossing gets one confirmation. A failed attempt ends the day."""
    first = orb.entry_intent(candidate, bars)
    if delay_seconds not in (0, 60):
        raise ValueError("unregistered delay")
    if first is None:
        return None
    # first.ready is crossing-bar end + one second. The next bar starts at its end.
    following = next((b for b in bars if b["t"] == first["ready"] - orb.SECOND), None)
    if following is None:
        raise ValueError("confirmation minute absent")
    if following["c"] <= candidate["high"]:
        return None
    ready = following["t"] + orb.MINUTE + orb.SECOND + delay_seconds * orb.SECOND
    if ready >= candidate["opening"] + 60 * orb.MINUTE:
        return None
    return {**first, "ready": ready, "expires": ready + 5 * orb.SECOND}


def cost_model(bps: int) -> dict:
    if bps not in (12, 24):
        raise ValueError("unregistered cost scenario")
    return {"version": 1, "market": "us", "slippage_bps": 0, "commission_bps": bps,
            "sell_tax_bps": 0, "sec_sell_bps": 0, "finra_per_share": 0, "finra_cap": 0}


def needs(sources: dict, quotes: dict, frozen: dict) -> dict:
    """Resolve all needed clocks before running account P&L, including stress delays."""
    windows = {}
    for day in frozen["evaluation_sessions"]:
        for ranking, entry, delay in (("rvol", confirmed_entry, 0), ("rvol", confirmed_entry, 60),
                                      ("rvol", orb.entry_intent, 0), ("turnover", orb.entry_intent, 0)):
            for candidate in orb.opening_candidates(sources, frozen["sessions"], day, ranking):
                bars = sources[(candidate["symbol"], day)]["bars"]
                intent = entry(candidate, bars, delay)
                if intent is None:
                    continue
                quote, missing = orb.quote_at(quotes, intent["symbol"], intent["ready"])
                if missing:
                    windows[tuple(missing.values())] = missing
                elif quote and intent["initial_stop"] < quote["ap"] <= intent["limit"]:
                    exit_at = orb.exit_clock(intent, quote, bars)["at"]
                    _, missing = orb.quote_at(quotes, intent["symbol"], exit_at)
                    if missing:
                        windows[tuple(missing.values())] = missing
    return {"schema": "intraday-confirmation-quote-plan-v1", "market": "us",
            "plan_sha256": hashlib.sha256(orb.json_bytes(frozen)).hexdigest(),
            "windows": sorted(windows.values(), key=lambda x: (x["start"], x["symbol"])),
            "live_order_eligible": False}


def compare(sources: dict, quotes: dict, frozen: dict) -> dict:
    if frozen != plan():
        raise ValueError("frozen plan differs")
    def path(ranking="rvol", entry=confirmed_entry, bps=12, delay=0, fraction=1):
        return orb.replay(sources, quotes, frozen, ranking, bps=bps, delay=delay,
                          liquidity_fraction=fraction, size_scale=100,
                          entry_rule=entry, cost_model=cost_model)
    paths = {"confirmed": path(), "turnover_baseline": path("turnover", orb.entry_intent),
             "previous_orb": path(entry=orb.entry_intent), "double_cost": path(bps=24),
             "delay_60s": path(delay=60), "half_size": path(fraction=.5)}
    complete = all(p["complete"] for p in paths.values())
    checks, lower = {}, None
    if complete:
        for p in paths.values():
            p["splits"] = {name: orb.split_metrics(p, days) for name, days in frozen["splits"].items()}
        chosen = paths["confirmed"]["splits"]["sealed_evaluation"]
        base = paths["turnover_baseline"]["splits"]["sealed_evaluation"]
        previous = paths["previous_orb"]["splits"]["sealed_evaluation"]
        days = frozen["splits"]["sealed_evaluation"]
        excess = [a["return_pct"] - b["return_pct"] for a, b in
                  zip(paths["confirmed"]["days"], paths["turnover_baseline"]["days"])
                  if a["day"] in days]
        lower = orb.paired_block_lower(excess)
        trades = [t for t in paths["confirmed"]["trades"] if t["day"] in days]
        pnl = sum(t["pnl"] for t in trades)
        by_name = [sum(t["pnl"] for t in trades if t["symbol"] == name) for name in orb.SYMBOLS]
        by_day = [sum(t["pnl"] for t in trades if t["day"] == day) for day in days]
        checks = {"positive_after_cost": chosen["return_pct"] > 0,
                  "beats_turnover": chosen["return_pct"] > base["return_pct"],
                  "beats_previous_orb": chosen["return_pct"] > previous["return_pct"],
                  "paired_lower_positive": lower is not None and lower > 0,
                  "no_higher_daily_risk": chosen["mdd_pct"] >= base["mdd_pct"]
                       and chosen["daily_sd_pct"] <= base["daily_sd_pct"],
                  "stress_positive": all(paths[k]["splits"]["sealed_evaluation"]["return_pct"] > 0
                       for k in ("double_cost", "delay_60s", "half_size")),
                  "not_single_name_dependent": pnl - max(by_name, default=0) > 0,
                  "not_single_day_dependent": pnl - max(by_day, default=0) > 0,
                  "no_ambiguous_fill": not any(t["ambiguity"] for t in trades)}
    return {"schema": "intraday-confirmation-comparison-v1", "policy": POLICY,
            "plan_sha256": hashlib.sha256(orb.json_bytes(frozen)).hexdigest(), "paths": paths,
            "cash_control_return_pct": 0, "checks": checks,
            "paired_daily_excess_lower_97_5_pct": lower,
            "verdict": ("blocked_missing_execution_inputs" if not complete else
                        "rejected_in_reconstruction" if not all(checks.values()) else
                        "conditional_pass_not_adopted_source_incomplete"),
            "existing_operating_engine_comparable": False, "live_order_eligible": False,
            "limitations": ["fixed_survivor_panel", "round_lot_scale_100_assumed",
                            "costs_and_delivery_lag_assumed", "minute_exit_not_tick_stop",
                            "emitted_bar_turnover_proxy_not_exact_trade_notional",
                            "2025_event_research_previously_seen_not_untouched_market_history",
                            "no_2026_operating_counterfactual"]}


def provenance(commit: str, sources: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    paths = ["scripts/measure/intraday_confirmation.py", "scripts/measure/intraday_orb_replay.py",
             "scripts/measure/intraday_orb_sources.py", "src/signal_desk/ingest/alpaca_research.py",
             "src/signal_desk/market_clock.py", "src/signal_desk/broker/execution.py",
             "tests/test_intraday_confirmation.py"]
    hashes = {}
    for name in paths:
        raw = (root / name).read_bytes()
        if subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root) != raw:
            raise ValueError("implementation not precommitted: " + name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    files = {str(p.relative_to(sources)): hashlib.sha256(p.read_bytes()).hexdigest()
             for kind in ("bars", "quotes") for p in sorted((sources / kind).rglob("*.json"))}
    return {"implementation_commit": commit, "code_sha256": hashes, "source_sha256": files}


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("command", choices=("freeze", "needs", "compare"))
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--sources", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--implementation-commit")
    p.add_argument("--no-price-evidence", type=Path)
    args = p.parse_args()
    if args.command == "freeze":
        digest = _save_new(args.plan, plan())
        print(json.dumps({"plan_sha256": digest}))
        return
    frozen = json.loads(args.plan.read_bytes())
    if frozen != plan() or not args.sources or not args.output or not args.implementation_commit:
        p.error("exact frozen plan, sources, new output and precommitted code required")
    prov = provenance(args.implementation_commit, args.sources)
    absent = verified_absence(args.no_price_evidence)
    if absent:
        prov["no_price_evidence_sha256"] = NO_PRICE_EVIDENCE_SHA256
        prov["absent_minutes_not_interpolated"] = [list(key) for key in sorted(absent)]
    bars = orb.load_bars(args.sources, known_absent=absent)
    quotes = orb.load_quotes(args.sources)
    expected = {(symbol, day) for symbol in orb.SYMBOLS for day in frozen["sessions"]}
    if set(bars) != expected:
        raise ValueError("source panel differs from frozen plan")
    result = (needs if args.command == "needs" else compare)(bars, quotes, frozen)
    result["provenance"] = prov
    digest = _save_new(args.output, result)
    print(json.dumps({"output": str(args.output), "sha256": digest,
                      "missing_windows": len(result.get("windows", [])), "verdict": result.get("verdict")}))


if __name__ == "__main__":
    main()
