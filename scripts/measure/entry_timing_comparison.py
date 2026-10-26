"""Fixed six-event timing diagnostic. Local historical data only, never orders."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

from scripts.measure import earnings_announcement_execution as ledger
from scripts.measure import intraday_orb_replay as orb
from scripts.measure.intraday_orb_sources import _save_new
from signal_desk import config
from signal_desk.ingest import alpaca_research as api

EVENT_SHA = "8372a7acf31db419d876d1efd16c9c976aa21d634a52354d8cb7c17ae1ba4772"
ROOT = Path(__file__).resolve().parents[2]
EVENTS = ROOT / "data/research/intraday-event-information-result-2026-10-26-v1.json"
DIRECTORY = ROOT / "data/research/entry-timing-2026-10-26-v1"
VARIANTS = {"base": ("eps", 12, 1, 0), "market": ("market", 12, 1, 0),
            "double_cost": ("eps", 24, 1, 0), "delay60": ("eps", 12, 1, 60),
            "half_size": ("eps", 12, .5, 0)}


def inputs() -> dict:
    raw = EVENTS.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EVENT_SHA:
        raise ValueError("fixed event result differs")
    result = json.loads(raw)
    if len(result["events"]) != 16 or sum(bool(e.get("eligible")) for e in result["events"]) != 6:
        raise ValueError("fixed cohort differs")
    return {"events": result["events"]}


def selected(events: dict, timing: str, delay: int = 0) -> list[dict]:
    if timing not in ("immediate", "opening") or delay not in (0, 60):
        raise ValueError("outside fixed timing diagnostic")
    rows = []
    for event in events["events"]:
        if event["status"] != "ready" or not event.get("eligible"):
            continue
        stamp = event["ready_ns"] if timing == "immediate" else (
            orb.session_clocks(event["opening_session"])[0] + 5 * orb.MINUTE)
        if stamp < event["ready_ns"]:
            raise ValueError("opening precedes information")
        rows.append({**event, "decision_ns": stamp + delay * ledger.SECOND})
    rows.sort(key=lambda e: (e["decision_ns"], e["id"]))
    days = [ledger.as_dt(e["decision_ns"]).astimezone(ledger.NY).date() for e in rows]
    if len(days) != len(set(days)):
        raise ValueError("overlapping positions outside fixed single-event ledger")
    return rows


def load_quotes(directory: Path) -> dict:
    sources = []
    for path in sorted(directory.glob("*.json")):
        source = json.loads(path.read_bytes())
        if source.get("schema") != "alpaca-historical-quote-window-v1":
            raise ValueError("wrong source schema")
        body = None
        if source["status"] in ("observed", "empty"):
            raw = base64.b64decode(source["raw_b64"], validate=True)
            if len(raw) != source["raw_bytes"] or hashlib.sha256(raw).hexdigest() != source["raw_sha256"]:
                raise ValueError("quote source hash mismatch")
            body = json.loads(raw)
        sources.append({**source, "body": body})
    return ledger.quote_index(sources)


def missing(events: dict, index: dict) -> list[dict]:
    """Acquire the causal reference→entry→exit sequence, including sized exits."""
    requests = {}
    for timing in ("immediate", "opening"):
        for policy, fee, fill, delay in VARIANTS.values():
            picks = selected(events, timing, delay)
            result = ledger.execute_selected(events, index, picks, year="2025", path=timing,
                policy=policy, fee_bp=fee, fill=fill, delay=delay, size_scale=100)
            # Visit all events even while the cash ledger is blocked by an earlier gap.
            for event in picks:
                symbol = "SPY" if policy == "market" else event["symbol"]
                ns = event["decision_ns"]
                ref, need = ledger.quote_or_need(index, ledger.query(symbol, ns), ns)
                if need:
                    requests[ledger.key(need)] = need
                if ref is None:
                    continue
                entry_ns = ((ref["ns"] + 2 * ledger.SECOND - 1) // ledger.SECOND) * ledger.SECOND
                entry, need = ledger.quote_or_need(index, ledger.query(symbol, entry_ns), entry_ns)
                if need:
                    requests[ledger.key(need)] = need
                if entry is None:
                    continue
                ns = entry["ns"] + 3600 * ledger.SECOND
                row = next((r for r in result["rows"] if r["id"] == event["id"]), {})
                quantity = (row.get("quantity", 1) + 99) // 100
                _, need = ledger.quote_or_need(index, ledger.query(symbol, ns), ns, "bp", quantity)
                if need:
                    requests[ledger.key(need)] = need
    # A failed preserved request is a blocker, not permission for an automatic retry.
    return [r for key, r in sorted(requests.items()) if key not in index]


def assess(events: dict, index: dict) -> dict:
    paths = {}
    for timing in ("immediate", "opening"):
        for variant, (policy, fee, fill, delay) in VARIANTS.items():
            paths[timing + ":" + variant] = ledger.execute_selected(events, index,
                selected(events, timing, delay), year="2025", path=timing, policy=policy,
                fee_bp=fee, fill=fill, delay=delay, size_scale=100)
    verdicts = {}
    for timing in ("immediate", "opening"):
        group = {v: paths[timing + ":" + v] for v in VARIANTS}
        if not all(p["complete"] for p in group.values()):
            verdict = "blocked_sources"
        elif (group["base"]["conditional_account_return_pct"] <= max(0, group["market"]["conditional_account_return_pct"])
              or any(group[v]["conditional_account_return_pct"] <= 0 for v in ("double_cost", "delay60", "half_size"))):
            verdict = "not_adopted_performance"
        else:
            verdict = "needs_independent_evaluation"
        verdicts[timing] = verdict
    return {"schema": "entry-timing-comparison-v1", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event_result_sha256": EVENT_SHA, "cohort_events": len(events["events"]),
            "eligible_events": sum(bool(e.get("eligible")) for e in events["events"]),
            "independent_holdout": False, "live_order_eligible": False,
            "paths": paths, "verdicts": verdicts,
            "source_windows": len(index), "source_raw_bytes": sum(s.get("raw_bytes", 0) for s in index.values()),
            "unknown_condition_rows": sum(s.get("unknown_condition_rows", 0) for s in index.values()),
            "source_hashes": {"|".join(k): s.get("raw_sha256") for k, s in sorted(index.items())}}


def public_report(result: dict) -> dict:
    """Only derived research metrics; never raw vendor responses or credentials."""
    fields = ("complete", "selected_events", "closed_trades", "conditional_account_return_pct",
              "closed_subset_pnl", "closed_subset_assumed_fees", "closed_subset_spread_drag",
              "unresolved_executions", "missing_input_events", "fee_bp_each_side_assumed",
              "extra_delay_seconds", "fill_fraction", "realized_close_only_drawdown_pct")
    row_fields = ("id", "issuer", "status", "decision_ns", "entry_ns", "exit_ns", "quantity",
                  "net_pnl", "trade_net_return_pct", "fees", "reference_lag_seconds", "exit_after_target_seconds")
    return {**{k: v for k, v in result.items() if k not in ("paths", "source_hashes")},
            "research_only": True, "starting_cash_usd": 10000,
            "hold_minutes": 60, "opening_delay_minutes": 5, "information_delay_minutes": 10,
            "shares_per_raw_size_unit_assumed": 100,
            "paths": {k: {**{f: p[f] for f in fields},
                           "rows": [{f: r[f] for f in row_fields if f in r} for r in p["rows"]]}
                      for k, p in result["paths"].items()}}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--public-output", type=Path)
    args = parser.parse_args()
    events = inputs()
    if args.collect:
        config.load_env()
        if not api.availability()["configured"]:
            raise SystemExit("Alpaca credentials missing; do not paste secrets into chat")
        import time
        for _ in range(25):  # Finite acquisition; never silently retry failed observations.
            index = load_quotes(DIRECTORY)
            requests = missing(events, index)
            if not requests:
                break
            for request in requests:
                source = api.quote_window(**request)
                name = hashlib.sha256(orb.json_bytes(request)).hexdigest() + ".json"
                _save_new(DIRECTORY / name, source)
                time.sleep(.35)
            print(json.dumps({"acquired_windows": len(requests)}), flush=True)
            if any(s["body"] is None for s in load_quotes(DIRECTORY).values()):
                break
    result = assess(events, load_quotes(DIRECTORY))
    result["plan_sha256"] = hashlib.sha256((ROOT / "docs/ENTRY-TIMING-COMPARISON-2026-10-26.md").read_bytes()).hexdigest()
    if args.output:
        _save_new(args.output, result)
    if args.public_output:
        _save_new(args.public_output, public_report(result))
    print(json.dumps({"verdicts": result["verdicts"], "source_windows": result["source_windows"],
        "paths": {k: {f: p[f] for f in ("complete", "closed_trades", "conditional_account_return_pct")}
                  for k, p in result["paths"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
