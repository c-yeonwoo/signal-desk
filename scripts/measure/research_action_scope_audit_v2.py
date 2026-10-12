"""Audit frozen seven development windows without promoting partial action data.

This summarizes the full ticker-window denominator. A selected verified event
does not assert that its holding interval contains no other action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT

LEDGER = ROOT / "docs/research-action-gap-ledger-2026-10-11.jsonl"
LEDGER_SHA256 = "7c3daa5edb5a1f891854cc5aa2e9f8c1635f4c21a16e2e808cbcac7fba9abfef"
REVIEWED = ROOT / "docs/research-reviewed-share-count-changes-2026-10-12.json"
REVIEWED_SHA256 = "9e28289ab11567de02f73cca2640ca768d50870bc8d2e1e5b73337beeb2ecea6"
REQUIRED_KINDS = (
    "cash_dividend", "split", "stock_dividend", "rights", "capital_reduction",
    "spin_off", "merger_exchange", "halt", "delisting", "symbol_change",
    "share_count_change",
)


def audit(ledger_path: Path = LEDGER, reviewed_path: Path = REVIEWED) -> dict:
    ledger_raw, reviewed_raw = ledger_path.read_bytes(), reviewed_path.read_bytes()
    if (hashlib.sha256(ledger_raw).hexdigest() != LEDGER_SHA256
            or hashlib.sha256(reviewed_raw).hexdigest() != REVIEWED_SHA256):
        raise ValueError("scope audit: frozen source digest differs")
    manifest, *rows = [json.loads(line) for line in ledger_raw.splitlines()]
    reviewed = json.loads(reviewed_raw)
    if (manifest.get("schema") != "research-action-gap-ledger-v1"
            or manifest.get("registered_verdict") is not False
            or reviewed.get("schema") != "research-reviewed-share-count-change-v1"
            or reviewed.get("coverage_state") != "partial"
            or reviewed.get("economic_return_pct") is not None
            or len(manifest.get("windows", [])) != 7
            or len(rows) != 1488
            or len(reviewed.get("facts", [])) != 7
            or reviewed.get("overlap_episode_refs") != 13):
        raise ValueError("scope audit: frozen study structure differs")
    checked = {(item["ticker"], item["listing_session"]) for item in reviewed["facts"]}
    if len(checked) != 7:
        raise ValueError("scope audit: duplicate selected issuer event")
    grouped: dict[str, list[dict]] = {item["window"]: [] for item in manifest["windows"]}
    for row in rows:
        window = row.get("window")
        if (window not in grouped or row.get("type") != "ticker_gap"
                or row.get("action_coverage") != "partial"):
            raise ValueError("scope audit: changed or unclassified frozen row")
        grouped[window].append(row)
    result_windows = []
    by_gap = Counter()
    reviewed_rows = 0
    for spec in manifest["windows"]:
        window, subset = spec["window"], grouped[spec["window"]]
        if len(subset) != spec["unique_tickers"]:
            raise ValueError("scope audit: window denominator differs")
        gaps = Counter(code for row in subset for code in row["gaps"])
        by_gap.update(gaps)
        matched = sum(any((row["ticker"], day) in checked
                          for day in row["listed_share_change_dates"])
                      for row in subset)
        reviewed_rows += matched
        result_windows.append({"window": window, "decision_start": spec["decision_start"],
                               "decision_end": spec["decision_end"],
                               "ticker_window_rows": len(subset), "action_coverage_partial": len(subset),
                               "action_coverage_complete": 0,
                               "rows_with_selected_reviewed_share_change": matched,
                               "rows_with_raw_price_gap": sum(bool(row["missing_raw_price_sessions"])
                                                              for row in subset),
                               "gaps_by_code": dict(sorted(gaps.items()))})
    return {"schema": "research-action-scope-audit-v2", "research_only": True,
            "ledger_sha256": LEDGER_SHA256, "selected_facts_sha256": REVIEWED_SHA256,
            "required_action_kinds": list(REQUIRED_KINDS),
            "ticker_window_denominator": len(rows), "windows": result_windows,
            "selected_issuer_events": len(checked),
            "selected_event_episode_refs": reviewed["overlap_episode_refs"],
            "rows_with_selected_reviewed_share_change": reviewed_rows,
            "coverage_complete_rows": 0, "coverage_partial_rows": len(rows),
            "gaps_by_code": dict(sorted(by_gap.items())),
            "policy_comparison_ready": False,
            "verified_economic_return_pct": None,
            "strict_pit_eligible": False, "registered_verdict": False,
            "live_order_eligible": False,
            "limitations": [
                "Selected share-count events do not enumerate all actions in any holding window",
                "Historical eight-factor inputs and decision-time availability remain unreconstructed",
                "No ticker-window was excluded from the frozen denominator",
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output),
                      "partial_rows": report["coverage_partial_rows"],
                      "windows": len(report["windows"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
