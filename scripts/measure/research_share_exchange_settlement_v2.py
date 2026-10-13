"""Purpose-specific research review of two frozen, selected merger originals.

This derives a new view. It neither edits the frozen v1 report nor certifies
all corporate actions in any historical strategy window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk.signals.research_share_exchange_settlement import RatioEvidence, value_exchange


SOURCE_REPORT = ROOT / "docs/research-share-exchange-source-audit-2026-10-12.json"
SOURCE_SHA256 = "4cad8e9b37c9cff3b443e2edb2b7f0654a81627fec8b390325b6fd2617e3fd5f"
LISTING_REPORT = ROOT / "docs/research-share-exchange-listing-audit-2026-10-12.json"
LISTING_SHA256 = "3516a0b6fe2e7e8c6b8e39c127a03df4faf8ca3496be035e021dc3ead294b5f0"
EXPECTED = {"010620": ("0.4059146", "329180"),
            "042670": ("0.1621707", "267270")}


def review(*, old_shares: int = 100, source_path: Path = SOURCE_REPORT,
           listing_path: Path = LISTING_REPORT) -> dict:
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("frozen source audit changed")
    source = json.loads(raw)
    listing_raw = listing_path.read_bytes()
    if hashlib.sha256(listing_raw).hexdigest() != LISTING_SHA256:
        raise ValueError("frozen listing audit changed")
    listing = json.loads(listing_raw)
    if (source.get("schema") != "research-share-exchange-source-audit-v1"
            or listing.get("schema") != "research-share-exchange-listing-audit-v1"
            or listing.get("underlying_terms_report_sha256") != SOURCE_SHA256):
        raise ValueError("share exchange audit lineage differs")
    cases = {}
    rows = source["cases"]
    if len(rows) != 2 or {row["terms"]["old_ticker"] for row in rows} != set(EXPECTED):
        raise ValueError("frozen merger case set differs")
    for row in rows:
        terms = row["terms"]
        ticker = terms["old_ticker"]
        ratio, successor = EXPECTED[ticker]
        if (terms["reported_shares_per_old_share"] != ratio
                or row["successor_ticker"] != successor
                or terms["fractional_cash_price_rule"] != "new_listing_session_KRX_close"
                or listing["cases"][ticker]["individual_share_delivery_confirmed"] is not False
                or row["action_coverage_state"] != "partial"):
            raise ValueError("selected terms or coverage differ")
        first = row["first_observed_successor_trade"]
        if not isinstance(first.get("close_krw"), int) or first["close_krw"] <= 0:
            raise ValueError("first successor traded close unavailable")
        evidence = RatioEvidence(ratio, "displayed_precision_unknown",
                                 f"DART:{row['dart_receipt']}")
        valuation = value_exchange(old_shares=old_shares, ratio=evidence,
                                   fractional_listing_price_krw=str(first["close_krw"]),
                                   successor_mark_price_krw=str(first["close_krw"]))
        cases[ticker] = {
            "successor_ticker": successor, "terms_source_id": evidence.source_id,
            "first_observed_successor_session": first["session"],
            "source_review_status": "selected_facts_reviewed_scope_partial",
            "simulated_entitlement_status": "conditional_on_hypothetical_record_holder",
            "economic_valuation_status": valuation["valuation_state"],
            "simulated_cash_availability_status": "fractional_payment_unconfirmed",
            "account_realized_status": "account_evidence_unavailable",
            "missing_by_purpose": {
                "exact_or_bounded_ratio": ["final contractual precision or documented display rule"],
                "simulated_cash_availability": ["final fractional amount and confirmed payment date"],
                "whole_window_total_return": ["complete corrections and action-kind coverage"],
                "account_realized": ["actual account share delivery and cash credit"]},
            "valuation": valuation,
        }
        if ticker == "010620":
            cases[ticker]["missing_by_purpose"]["simulated_tradeability"] = [
                "new-share batch listing notice/date"]
    return {"schema": "research-share-exchange-settlement-review-v2",
            "source_report_sha256": SOURCE_SHA256,
            "listing_report_sha256": LISTING_SHA256,
            "research_only": True, "strict_pit_eligible": False,
            "registered_verdict": False, "live_order_eligible": False,
            "whole_window_total_return_pct": None,
            "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hypothetical-old-shares", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = review(old_shares=args.hypothetical_old_shares)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
