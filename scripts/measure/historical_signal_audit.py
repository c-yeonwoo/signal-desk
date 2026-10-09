"""Audit all locally recorded signals before the registered evaluation period.

Example:
  .venv/bin/python scripts/measure/historical_signal_audit.py --market kr \
    --output data/research/historical_signal_audit_kr.json

No provider calls, live decisions, orders, or registered harness runs occur here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tomllib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from signal_desk.signals.historical_audit import (  # noqa: E402
    MAJOR_KR_TICKERS, MAJOR_US_TICKERS, audit_planned_casebook, audit_snapshots,
    inventory_recorded_inputs,
    plan_recorded_casebook, select_forensic_case_keys,
)
from signal_desk import market_clock  # noqa: E402


def _registered_start() -> str:
    registered = tomllib.loads((ROOT / "docs" / "preregistered.toml").read_text(encoding="utf-8"))
    dates = [str(item["registered_at"]) for item in registered.get("looks", [])
             if item.get("registered_at")]
    for family in registered.get("families", []):
        dates.extend(str(item["registered_at"]) for item in family.get("looks", [])
                     if item.get("registered_at"))
    if not dates:
        raise ValueError("registered-period protection has no starting date")
    return min(dates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("kr", "us"), default="kr")
    parser.add_argument("--signals", type=Path, default=ROOT / "data/cache/signal_history.parquet")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--bundle", type=Path, help="Admin-only exported ZIP, read without extraction")
    parser.add_argument("--forensic", action="store_true",
                        help="List protected-period signal changes and fixed majors as cases only; no aggregate score")
    parser.add_argument("--inventory-only", action="store_true",
                        help="Input coverage and PIT limitations only; never read forward outcomes")
    parser.add_argument("--casebook-plan", action="store_true",
                        help="Freeze outcome-blind weekly cases from signal metadata only")
    parser.add_argument("--casebook-audit", type=Path,
                        help="Replay only the cases in an exact frozen casebook plan")
    parser.add_argument("--output", type=Path, help="Optional JSON artifact; existing file is never overwritten")
    args = parser.parse_args()
    if sum((args.casebook_plan, args.inventory_only, args.forensic,
            args.casebook_audit is not None)) > 1:
        parser.error("choose only one audit mode")
    if args.casebook_audit and not args.output:
        parser.error("--casebook-audit requires --output for a local case-level artifact")
    if args.casebook_audit and args.output.exists():
        parser.error("--output already exists; casebook audit never overwrites an artifact")
    started = time.perf_counter()
    prices_path = args.prices or ROOT / "data/cache" / ("prices.parquet" if args.market == "kr" else "us_prices.parquet")
    if args.bundle:
        with ZipFile(args.bundle) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            if manifest.get("schema") != "historical-inputs-v1" or manifest.get("market") != args.market:
                raise ValueError("bundle market or schema mismatch")
            signal_bytes, price_bytes = bundle.read("signals.parquet"), bundle.read("prices.parquet")
        if hashlib.sha256(signal_bytes).hexdigest() != manifest["signals_sha256"]:
            raise ValueError("signal input digest mismatch")
        if hashlib.sha256(price_bytes).hexdigest() != manifest["prices_sha256"]:
            raise ValueError("price input digest mismatch")
        frame = pd.read_parquet(BytesIO(signal_bytes))
        prices = None if args.casebook_plan else pd.read_parquet(BytesIO(price_bytes))
        input_hashes = {"signal_sha256": manifest["signals_sha256"],
                        "price_sha256": manifest["prices_sha256"],
                        "bundle_sha256": hashlib.sha256(args.bundle.read_bytes()).hexdigest()}
    else:
        frame = pd.read_parquet(args.signals)
        prices = None if args.casebook_plan else pd.read_parquet(prices_path)
        input_hashes = {"signal_sha256": hashlib.sha256(args.signals.read_bytes()).hexdigest(),
                        "price_sha256": hashlib.sha256(prices_path.read_bytes()).hexdigest()}
    if "market" in frame:
        frame = frame[frame["market"].fillna("kr").astype(str) == args.market]
    elif args.market != "kr" and not args.bundle:
        # The default local file mixes markets; the authenticated bundle is
        # already market-scoped and its market was verified above.
        frame = frame.iloc[0:0]
    protected_start = _registered_start()
    if args.casebook_audit:
        plan = json.loads(args.casebook_audit.read_text(encoding="utf-8"))
        result = audit_planned_casebook(
            frame, prices, plan=plan, market=args.market,
            signal_sha256=input_hashes["signal_sha256"],
            protected_start=protected_start,
        )
        result["inputs"] = input_hashes
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({"market": args.market, "plan_sha256": result["plan_sha256"],
                          "case_count": result["case_count"], "source_level": result["source_level"],
                          "warning": result["warning"], "output": str(args.output)}, ensure_ascii=False))
        return
    if args.casebook_plan:
        result = plan_recorded_casebook(frame, market=args.market,
                                        protected_start=protected_start,
                                        signal_sha256=input_hashes["signal_sha256"])
        print(json.dumps({"market": args.market, "plan_sha256": result["plan_sha256"],
                          "signal_rows_seen": result["signal_rows_seen"],
                          "excluded_rows": result["excluded_rows"],
                          "case_slots": len(result["slots"]),
                          "warning": result["limitations"][1]}, ensure_ascii=False, indent=2))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return
    if args.inventory_only:
        if args.forensic:
            parser.error("--inventory-only and --forensic cannot be combined")
        result = inventory_recorded_inputs(frame, prices, market=args.market,
                                           protected_start=protected_start)
        result["inputs"] = input_hashes
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return
    before = len(frame)
    protected = frame[frame["date"].astype(str) >= protected_start]
    frame = frame[frame["date"].astype(str) < protected_start]
    majors = MAJOR_KR_TICKERS if args.market == "kr" else MAJOR_US_TICKERS
    result = audit_snapshots(frame, prices, market=args.market, major_tickers=majors)
    result["protection"] = {"registered_period_from": protected_start,
                            "excluded_signal_rows": before - len(frame)}
    result["inputs"] = input_hashes
    if args.forensic and not protected.empty:
        # Freeze incident keys from saved signals before any protected price outcome.
        keys = select_forensic_case_keys(protected, market=args.market, major_tickers=majors)
        cases = audit_snapshots(protected, prices, market=args.market, major_tickers=majors,
                                include_aggregates=False, case_keys=keys)
        selected = cases["rows"]
        result["forensic"] = {
            "source_level": cases["source_level"], "selection": "kind_change_or_abs_score_delta_ge_0.5_or_fixed_major",
            "signal_rows_seen": len(protected), "selected_cases": len(selected),
            "excluded_non_session_rows": int((~protected["date"].astype(str).map(
                lambda day: market_clock.is_session(args.market, day))).sum()),
            "rows": selected,
            "warning": "개별 사고 조사만 허용; 사전등록 기간의 집계·튜닝·승격 근거 아님",
        }
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    summary = {key: value["5"] for key, value in result["summary"].items()}
    print(json.dumps({"market": args.market, "signal_dates": result.get("signal_dates"),
                      "price_data_to": result.get("price_data_to"), "signal_rows": result.get("signal_rows"),
                      "snapshot_gap_rows": result.get("snapshot_gap_rows"),
                      "h5_summary": summary, "protection": result["protection"],
                      "forensic_case_count": result.get("forensic", {}).get("selected_cases"),
                      "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"research artifact: {args.output}")


if __name__ == "__main__":
    main()
