"""Read-only, fixed seven-window share-exchange readiness overlay.

This is a scope report, not a return calculation or a holder entitlement.
The selected corporate-action examples do not certify an action-free universe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT
from scripts.measure.research_share_exchange_settlement_v2 import review as review_cases


LEDGER = ROOT / "docs/research-action-gap-ledger-2026-10-11.jsonl"
LEDGER_SHA256 = "7c3daa5edb5a1f891854cc5aa2e9f8c1635f4c21a16e2e808cbcac7fba9abfef"
SOURCE = ROOT / "docs/research-share-exchange-source-audit-2026-10-12.json"
EVENT_DATES = {"010620": "2025-12-01", "042670": "2026-01-01"}


def build() -> dict:
    raw = LEDGER.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LEDGER_SHA256:
        raise ValueError("frozen seven-window ledger changed")
    records = [json.loads(line) for line in raw.splitlines()]
    manifest, rows = records[0], records[1:]
    windows = manifest.get("windows")
    if (manifest.get("schema") != "research-action-gap-ledger-v1"
            or len(windows or ()) != 7 or len(rows) != 1488
            or {row["window"] for row in rows} != {window["window"] for window in windows}
            or any(row["action_coverage"] != "partial" for row in rows)):
        raise ValueError("unexpected fixed development denominator or coverage")
    selected = review_cases()
    original = json.loads(SOURCE.read_bytes())
    if {row["terms"]["old_ticker"]: row["terms"]["merger_effective_on"]
            for row in original["cases"]} != EVENT_DATES:
        raise ValueError("selected event dates changed")
    counts = Counter(row["window"] for row in rows)
    reports = []
    for window in windows:
        name = window["window"]
        possible = []
        for row in rows:
            ticker = row["ticker"]
            effective = EVENT_DATES.get(ticker)
            if row["window"] != name or effective is None:
                continue
            if row["required_scope_start"] <= effective <= row["required_scope_end"]:
                possible.append({"old_ticker": ticker,
                    "successor_ticker": selected["cases"][ticker]["successor_ticker"],
                    "effective_on": effective,
                    "scope_relation": "within_candidate_research_scope_not_proven_held",
                    "ratio_state": selected["cases"][ticker]["economic_valuation_status"],
                    "simulated_holding_eligibility": "unverified"})
        reports.append({"window": name, "candidate_ticker_rows": counts[name],
            "rows_with_partial_action_coverage": counts[name],
            "selected_possible_exchange_cases": sorted(possible, key=lambda item: item["old_ticker"]),
            "certified_total_return_pct": None,
            "missing_by_purpose": {
                "whole_window_total_return": ["complete action-kind/effective-date/correction coverage for every traded holding"],
                "selected_exchange_simulation": ["holder eligibility and final ratio/rounding/payment evidence where applicable"],
                "account_realized": ["account delivery, cash credit and executions"]}})
    return {"schema": "research-share-exchange-seven-window-readiness-v1",
            "ledger_sha256": LEDGER_SHA256,
            "selected_source_sha256": selected["source_report_sha256"],
            "research_only": True, "registered_verdict": False,
            "live_order_eligible": False, "strict_pit_eligible": False,
            "window_count": 7, "candidate_ticker_rows": len(rows),
            "certified_total_return_pct": None, "windows": reports}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(build(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(result + "\n")
    else:
        print(result)


if __name__ == "__main__":
    main()
