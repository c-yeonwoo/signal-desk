"""Freeze issuer-guidance events, collect bounded minute pages, replay offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from scripts.measure.contract_event_backtest import _identities
from scripts.measure.contract_event_sources import _rows, _save, collect_minutes, load_archive
from scripts.measure.earnings_event_sources import PERIODS, parse_listing
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk import market_clock
from signal_desk.signals.contract_event import decode_minutes
from signal_desk.signals.earnings_event import execution_event, facts, replay, select_events

PLAN = ROOT / "docs/EARNINGS-GUIDANCE-BACKTEST-2026-10-17.md"
POLICIES = ("fundamental", "reaction", "market", "cash")
VALIDATION = ("2026-05", "2026-06", "2026-07")


def derive(source: Path) -> tuple[list[dict], list[dict], dict]:
    manifest, files = load_archive(source, "earnings-guidance-originals-v1")
    census = []
    for group, total in manifest["census_counts"].items():
        kind = "guidance" if group == "baseline_guidance" else group
        start, end = PERIODS[group]
        rows = []
        for name, raw in files.items():
            if name.startswith(f"raw/{group}/index-"):
                count, batch = parse_listing(raw.decode(), start=start, end=end)
                if count != total:
                    raise ValueError("filing census count differs")
                rows.extend({**row, "kind": kind, **({"source_group": group} if "query_periods" in manifest else {})} for row in batch)
        if len(rows) != total or len({row["receipt"] for row in rows}) != total:
            raise ValueError("filing pagination incomplete")
        census.extend(rows)
    by_receipt = {row["receipt"]: row for row in census}
    ledger = manifest["ledger"]
    if len(ledger) != len(census) or {row["receipt"] for row in ledger} != set(by_receipt):
        raise ValueError("filing ledger incomplete")
    parsed, reviewed = {}, []
    for event in ledger:
        if any(event[key] != value for key, value in by_receipt[event["receipt"]].items()):
            raise ValueError("filing ledger identity differs from raw index")
        event = dict(event)
        if event["status"] == "original_preserved":
            raw = files[event["source_file"]]
            if hashlib.sha256(raw).hexdigest() != event["source_sha256"]:
                raise ValueError("financial original hash differs")
            try:
                parsed[event["receipt"]] = facts(_rows(raw.decode()), event["kind"])
            except ValueError as exc:
                event["parse_error"] = str(exc)
        reviewed.append(event)
    selected, audit = select_events(reviewed, parsed, market_clock.is_session)
    return selected, audit, {"source_census": manifest["census_counts"],
                             "original_statuses": dict(Counter(row["status"] for row in reviewed)),
                             "parse_errors": dict(Counter(row["parse_error"] for row in reviewed if row.get("parse_error"))),
                             "parsed_filings": len(parsed),
                             "selection_reasons": dict(Counter(row["reason"] for row in audit)),
                             "selected_by_month": dict(Counter(row["execution_session"][:7] for row in selected))}


def freeze(source: Path, output: Path) -> dict:
    events, audit, census = derive(source)
    _save(output, {}, {"schema": "earnings-guidance-events-v1", "events": events,
                       "audit": audit, "census": census,
                       "original_archive_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "strict_pit_eligible": False, "live_order_eligible": False})
    return {**census, "selected_events": len(events)}


def verdict(months: dict) -> dict:
    primary = [months[month]["75bp_full_delay0"]["fundamental"] for month in VALIDATION]
    checks = {"all_primary_paths_complete": all(path["state"] == "complete" for path in primary)}
    if not checks["all_primary_paths_complete"]:
        incomplete = any(path["state"] not in ("complete", "no_eligible_events") for path in primary)
        return {"adoption": "rejected", "reason": "incomplete_validation_prices" if incomplete else "insufficient_validation_events",
                "validation_event_count": sum(path.get("events", 0) for path in primary),
                "validation_trade_count": sum(path.get("trade_count", 0) for path in primary),
                "checks": checks, "live_order_eligible": False}
    trades = [trade for path in primary for trade in path["trades"]]
    checks.update(at_least_12_event_days=sum(path["events"] for path in primary) >= 12,
                  at_least_8_trades=len(trades) >= 8,
                  at_least_6_issuers=len({row["ticker"] for row in trades}) >= 6,
                  at_least_2_trades_every_month=min(path["trade_count"] for path in primary) >= 2,
                  positive_every_validation_month=all(path["net_return_pct"] > 0 for path in primary))
    for scenario in ("120bp_full_delay0", "75bp_full_delay2"):
        paths = [months[month][scenario]["fundamental"] for month in VALIDATION]
        checks[scenario + "_positive"] = (statistics.mean(path["net_return_pct"] for path in paths) > 0
                                           if all(path["state"] == "complete" for path in paths) else None)
    for policy in ("reaction", "market", "cash"):
        controls = [months[month]["75bp_full_delay0"][policy] for month in VALIDATION]
        checks["excess_over_" + policy] = (statistics.mean(path["net_return_pct"] - control["net_return_pct"]
                                                           for path, control in zip(primary, controls)) > 0
                                            if all(path["state"] == "complete" for path in controls) else None)
    positive = defaultdict(float)
    for trade in trades:
        positive[trade["ticker"]] += max(0, trade["net_pnl"])
    total = sum(positive.values())
    checks["positive_pnl_not_concentrated"] = max(positive.values(), default=0) / total <= .5 if total else False
    return {"adoption": "shadow_candidate" if all(checks.values()) else "rejected",
            "reason": "fixed_screen_passed" if all(checks.values()) else "fixed_screen_failed",
            "checks": checks, "live_order_eligible": False}


def run(source: Path, frozen: Path, minute: Path, data_dir: Path) -> dict:
    events, audit, census = derive(source)
    cohort, _ = load_archive(frozen, "earnings-guidance-events-v1")
    if (cohort["original_archive_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest()
            or cohort["events"] != events or cohort["audit"] != audit or cohort["census"] != census):
        raise ValueError("frozen cohort differs from original source recomputation")
    identities = _identities([execution_event(event) for event in events], data_dir) if events else {}
    minutes, files = load_archive(minute, "contract-event-minutes-v1")
    if minutes["event_archive_sha256"] != hashlib.sha256(frozen.read_bytes()).hexdigest():
        raise ValueError("minute pages belong to another frozen cohort")
    observations = {(row["receipt"], row["ticker"]): row for row in minutes["observations"]}
    expected = {(event["receipt"], ticker) for event in events for ticker in (event["ticker"], "069500")}
    if set(observations) != expected or len(observations) != len(minutes["observations"]):
        raise ValueError("minute event pairs missing or duplicated")
    records, diagnostics = defaultdict(list), []
    for event in events:
        record = {"event": execution_event(event)}
        try:
            for key, ticker in (("stock", event["ticker"]), ("control", "069500")):
                observed = observations[event["receipt"], ticker]
                params = observed["request"]
                if (params["FID_INPUT_ISCD"] != ticker or params["FID_INPUT_DATE_1"] != event["execution_session"].replace("-", "")
                        or params["FID_FAKE_TICK_INCU_YN"] != ""):
                    raise ValueError("minute request identity/date mismatch")
                body = json.loads(files[observed["source_file"]])
                if observed["status"] != "observed" or not isinstance(body, dict) or body.get("rt_cd") != "0":
                    raise ValueError("minute provider failed")
                if key == "stock" and "".join((body.get("output1", {}).get("hts_kor_isnm") or "").split()) != "".join(event["issuer"].split()):
                    raise ValueError("minute security name differs")
                record[key] = decode_minutes(body, event["execution_session"])
        except (ValueError, KeyError, TypeError) as exc:
            record["source_error"] = str(exc)
        records[event["execution_session"][:7]].append(record)
        diagnostics.append({**event, "source_error": record.get("source_error"),
                            "stock_bars": len(record.get("stock", {})), "control_bars": len(record.get("control", {}))})
    months = {}
    for month in (f"2026-{number:02d}" for number in range(1, 8)):
        sample, scenarios = records[month], {}
        for cost in (45, 75, 120):
            for fill, fraction in (("full", 1), ("half", .5), ("none", 0)):
                for delay in (0, 2):
                    scenarios[f"{cost}bp_{fill}_delay{delay}"] = {
                        policy: replay(sample, policy, cost_bps=cost, fill_fraction=fraction,
                                       additional_delay_minutes=delay) if sample else
                        {"state": "no_eligible_events", "events": 0, "trade_count": 0, "net_return_pct": None}
                        for policy in POLICIES}
        months[month] = scenarios
    plan = PLAN.read_text()
    if plan.count("\n## 결과\n") != 1:
        raise ValueError("plan/result boundary differs")
    return {"schema": "earnings-guidance-backtest-v1", "phase": "historical_reconstruction",
            "census": census, "events": diagnostics, "months": months, "adoption": verdict(months),
            "source_archives_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (source, frozen, minute)},
            "identity_sources_sha256": identities,
            "plan_sha256": hashlib.sha256(plan.split("\n## 결과\n", 1)[0].encode()).hexdigest(),
            "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                            ("scripts/measure/earnings_event_sources.py", "scripts/measure/earnings_event_backtest.py",
                             "scripts/measure/contract_event_sources.py", "src/signal_desk/signals/contract_event.py",
                             "src/signal_desk/signals/earnings_event.py")},
            "strict_pit_eligible": False, "registered_verdict": False, "live_order_eligible": False,
            "limitations": ["Issuer guidance is not analyst consensus or a calibrated price expectation.",
                            "Historical provider vintages, first press release, order book/VI/fills are unverified.",
                            "Unsupported periods/units and intervening unknown guidance are retained as missing.",
                            "Contemporaneous independent KOSDAQ security lineage is not fully certified.",
                            "Previously inspected market periods are not a pristine historical holdout."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "minutes", "run"))
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--event-archive", type=Path)
    parser.add_argument("--minute-archive", type=Path)
    parser.add_argument("--reuse-source", type=Path)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("immutable outputs cannot be overwritten")
    if args.phase == "freeze":
        if not args.source_archive:
            parser.error("original archive required")
        result = freeze(args.source_archive, args.output)
    elif args.phase == "minutes":
        if not args.event_archive:
            parser.error("frozen event archive required")
        result = collect_minutes(args.event_archive, args.output, args.reuse_source,
                                 event_schema="earnings-guidance-events-v1")
    else:
        if not all((args.source_archive, args.event_archive, args.minute_archive)):
            parser.error("original/event/minute archives required")
        result = run(args.source_archive, args.event_archive, args.minute_archive, args.data_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        result = {"census": result["census"], "adoption": result["adoption"]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
