"""Replay one frozen official-filing reaction policy from archived sources."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.contract_event_sources import load_archive, parse_contract, parse_index
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk.signals.contract_event import POLICIES, adoption, decode_minutes, run_path

PLAN = ROOT / "docs/CONTRACT-EVENT-BACKTEST-2026-10-17.md"


def plan_digest() -> str:
    text = PLAN.read_text()
    marker = "\n## 결과\n"
    if text.count(marker) != 1:
        raise ValueError("frozen plan/result boundary missing or ambiguous")
    return hashlib.sha256(text.split(marker, 1)[0].encode()).hexdigest()


def verified_events(path: Path) -> list[dict]:
    manifest, files = load_archive(path, "contract-event-filings-v1")
    rows = []
    total = manifest["census_count"]
    for name, source in files.items():
        if name.startswith("raw/index-"):
            count, events = parse_index(source.decode())
            if count != total:
                raise ValueError("index pagination count differs")
            rows.extend(events)
    by_receipt = {row["receipt"]: row for row in rows}
    if len(rows) != total or len(by_receipt) != total:
        raise ValueError("incomplete filing census")
    events = manifest["events"]
    if not events or [row["session"] for row in events] != sorted({row["session"] for row in events}):
        raise ValueError("frozen event days duplicate or unordered")
    for event in events:
        original = by_receipt.get(event["receipt"])
        if not original or any(event[key] != value for key, value in original.items()):
            raise ValueError("event publication/issuer differs from original index")
        body = files[event["source_file"]]
        if hashlib.sha256(body).hexdigest() != event["source_sha256"]:
            raise ValueError("selected original hash mismatch")
        facts = parse_contract(body.decode())
        if any(event[key] != value for key, value in facts.items()) or facts["computed_revenue_ratio_pct"] < 10:
            raise ValueError("event facts differ from original contract")
    return events


def _identities(events: list[dict], data_dir: Path) -> dict:
    """Bind the five-digit KIND company code to that day's listed common share."""
    needed = {event["session"] for event in events}
    identities, archives = {}, {}
    for name in ("krx-daily-2026-01-05-v1.zip", "krx-daily-2026-06-07-v1.zip"):
        path = data_dir / name
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for member, entry in manifest["entries"].items():
                if entry["session"] not in needed:
                    continue
                raw = archive.read(member)
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("daily identity source hash mismatch")
                identities[entry["session"]] = {row["ISU_CD"]: row["ISU_NM"]
                                                 for row in json.loads(raw)["OutBlock_1"]}
        archives[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for event in events:
        if event.get("market_segment") != "kospi":
            continue
        source_name = (identities.get(event["session"]) or {}).get(event["ticker"])
        if not source_name or "".join(source_name.split()) != "".join(event["issuer"].split()):
            raise ValueError("KIND issuer does not match contemporaneous KRX security")
    return archives


def run(event_archive: Path, minute_archive: Path, data_dir: Path) -> dict:
    events = verified_events(event_archive)
    identity_sources = _identities(events, data_dir)
    manifest, files = load_archive(minute_archive, "contract-event-minutes-v1")
    if manifest["event_archive_sha256"] != hashlib.sha256(event_archive.read_bytes()).hexdigest():
        raise ValueError("minute archive is not bound to the frozen event cohort")
    observations = {(row["receipt"], row["ticker"]): row for row in manifest["observations"]}
    expected = {(event["receipt"], ticker) for event in events for ticker in (event["ticker"], "069500")}
    if set(observations) != expected or len(observations) != len(manifest["observations"]):
        raise ValueError("minute archive event pairs missing or duplicated")
    records, census = defaultdict(list), Counter()
    event_diagnostics = []
    for event in events:
        record = {"event": event}
        try:
            for key, ticker in (("stock", event["ticker"]), ("control", "069500")):
                observation = observations[event["receipt"], ticker]
                params = observation["request"]
                if (params["FID_INPUT_ISCD"] != ticker
                        or params["FID_INPUT_DATE_1"] != event["session"].replace("-", "")
                        or params["FID_FAKE_TICK_INCU_YN"] != ""):
                    raise ValueError("minute request identity/date mismatch")
                body = json.loads(files[observation["source_file"]])
                if observation["status"] != "observed" or not isinstance(body, dict) or body.get("rt_cd") != "0":
                    raise ValueError("provider_failed")
                if key == "stock":
                    source_name = (body.get("output1") or {}).get("hts_kor_isnm") if isinstance(body, dict) else None
                    if not source_name or "".join(source_name.split()) != "".join(event["issuer"].split()):
                        raise ValueError("KIS stock name differs from filing issuer")
                record[key] = decode_minutes(body, event["session"])
                census["minute_bars"] += len(record[key])
        except (ValueError, KeyError, TypeError) as exc:
            record["source_error"] = str(exc)
            census["source_errors"] += 1
        records[event["session"][:7]].append(record)
        event_diagnostics.append({**event, "source_error": record.get("source_error"),
                                  "stock_bar_count": len(record.get("stock") or {}),
                                  "control_bar_count": len(record.get("control") or {})})
    months = {}
    for month, sample in records.items():
        scenarios = {}
        for cost in (45, 75, 120):
            for fill_name, fraction in (("full", 1), ("half", 0.5), ("none", 0)):
                for delay in (0, 2):
                    scenarios[f"{cost}bp_{fill_name}_delay{delay}"] = {
                        policy: run_path(sample, policy, cost_bps=cost, fill_fraction=fraction,
                                         additional_delay_minutes=delay) for policy in POLICIES}
        months[month] = scenarios
    return {"schema": "contract-event-backtest-v1", "phase": "historical_reconstruction",
            "registered_verdict": False, "strict_pit_eligible": False, "live_order_eligible": False,
            "adoption": adoption({month: rows for month, rows in months.items() if month != "2026-05"}),
            "census": {**dict(census), "events": len(events), "distinct_dates": len({row["session"] for row in events}),
                       "distinct_issuers": len({row["ticker"] for row in events})},
            "events": event_diagnostics, "months": months,
            "source_archives_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (event_archive, minute_archive)},
            "identity_sources_sha256": identity_sources,
            "plan_sha256": plan_digest(),
            "code_sha256": {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                            for relative in ("src/signal_desk/signals/contract_event.py",
                                             "scripts/measure/contract_event_sources.py",
                                             "scripts/measure/contract_event_backtest.py")},
            "limitations": ["Filing publication is minute precision; earlier press/publication is unverified.",
                            "Historical sources were observed now, not their original served vintages.",
                            "Auction/VI, bid/ask, actual fills and historical fee schedules are not certified.",
                            "Full/half/zero fills and costs are declared models, not execution guarantees.",
                            "Market control is KODEX200, not a sector/risk-matched control.",
                            "Same-issuer events and overlapping market regimes are not independent trials."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-archive", type=Path, required=True)
    parser.add_argument("--minute-archive", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("existing backtest results cannot be overwritten")
    result = run(args.event_archive, args.minute_archive, args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print(json.dumps({"output": str(args.output), "census": result["census"],
                      "adoption": result["adoption"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
