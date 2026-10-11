"""Research-only *hypothetical* entitlement formulas, never realized returns.

Run against frozen DART/KRX archives. This does not certify a holder's record-date
eligibility, account credit, payment date, fill price, fees, tax or total return.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.krx_price_replay_pilot import ROOT
from scripts.measure.research_action_price_lineage import CASES, review


FRACTION_REFERENCE_RULE = "주권상장일에 한국거래소에서 거래되는 종가"


def _decimal(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("entitlement terms must be decimal strings, never floats")
    try:
        result = Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise ValueError("invalid entitlement term") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("entitlement term must be finite and positive")
    return result


def _as_string(value: Decimal) -> str:
    return format(value, "f")


def hypothetical_entitlement(*, old_shares: int, consideration: str,
                             successor_close_krw: int | None) -> dict:
    """Value stated terms at a successor's first traded close, with no payout rounding.

    A cash-exchange amount is only scheduled consideration. A share-exchange
    value is a reference formula; it is not the holder's tradeable wealth.
    """
    if isinstance(old_shares, bool) or not isinstance(old_shares, int) or not 1 <= old_shares <= 1_000_000_000:
        raise ValueError("hypothetical old shares must be a positive integer")
    term = _decimal(consideration)
    base = {"hypothetical_old_shares": old_shares,
            "record_date_eligibility_verified": False,
            "account_credit_verified": False,
            "actual_new_shares": None,
            "actual_cash_credit_krw": None,
            "realized_total_return_pct": None}
    if successor_close_krw is None:
        return {**base, "kind": "cash_exchange",
                "scheduled_gross_cash_formula_krw": _as_string(term * old_shares)}
    if (isinstance(successor_close_krw, bool) or not isinstance(successor_close_krw, int)
            or successor_close_krw <= 0 or term >= 1):
        raise ValueError("merger terms need a positive first traded close and fractional ratio")
    exact_new_shares = term * old_shares
    whole = int(exact_new_shares.to_integral_value(rounding=ROUND_FLOOR))
    fraction = exact_new_shares - whole
    price = Decimal(successor_close_krw)
    return {**base, "kind": "share_exchange",
            "whole_new_shares_formula": whole,
            "fractional_new_share_formula": _as_string(fraction),
            "first_traded_successor_close_krw": successor_close_krw,
            "unrounded_fractional_cash_reference_krw": _as_string(fraction * price),
            "unrounded_gross_reference_value_krw": _as_string(exact_new_shares * price),
            "cash_rounding_and_actual_payment_verified": False}


def _assert_fraction_rule(data_dir: Path, case) -> None:
    archive_name, _ = case.dart_archive
    with ZipFile(data_dir / archive_name) as archive:
        with ZipFile(io.BytesIO(archive.read(f"raw/doc/{case.receipt}.zip"))) as original:
            raw = original.read(f"{case.receipt}.xml")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", raw.decode("utf-8")))
    if FRACTION_REFERENCE_RULE not in text:
        raise ValueError("DART original does not confirm first-listing-close fraction rule")


def review_hypothetical(data_dir: Path, old_shares: int) -> dict:
    """Reuse pinned source hashes and traded-bar checks from price lineage."""
    lineage = review(data_dir)
    cases = []
    for case, observed in zip(CASES, lineage["cases"], strict=True):
        if observed["old_ticker"] != case.old_ticker or observed["successor_ticker"] != case.successor_ticker:
            raise ValueError("lineage case identity differs")
        first_traded = observed["successor_first_five_trading_sessions"]
        if case.successor_ticker:
            _assert_fraction_rule(data_dir, case)
        scenario = hypothetical_entitlement(
            old_shares=old_shares, consideration=case.consideration_text,
            successor_close_krw=first_traded[0]["close_krw"] if first_traded else None)
        cases.append({"old_ticker": case.old_ticker,
                      "successor_ticker": case.successor_ticker,
                      "terms_receipt": case.receipt,
                      "terms_original_sha256": observed["dart_original_sha256"],
                      "reference_session": first_traded[0]["session"] if first_traded else None,
                      **scenario})
    return {"schema": "research-action-hypothetical-entitlement-v1",
            "research_only": True, "strict_pit_eligible": False,
            "registered_verdict": False, "cases": cases,
            "limitations": ["Hypothetical record-date holder, not an observed account",
                            "Fraction cash is an unrounded filing formula, not confirmed credit",
                            "No fees, tax, sale execution or realized return is computed"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--hypothetical-old-shares", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(review_hypothetical(args.data_dir, args.hypothetical_old_shares),
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
