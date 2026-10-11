"""A filing formula is not an observed account credit or realized return."""

import io
from decimal import Decimal
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from scripts.measure import research_action_entitlements as entitlements


def test_share_formula_keeps_whole_and_fractional_value_separate():
    row = entitlements.hypothetical_entitlement(
        old_shares=10, consideration="0.1621707", successor_close_krw=117800)
    assert row["whole_new_shares_formula"] == 1
    assert Decimal(row["fractional_new_share_formula"]) == Decimal("0.6217070")
    assert Decimal(row["unrounded_fractional_cash_reference_krw"]) == (
        Decimal("0.6217070") * 117800)
    assert Decimal(row["unrounded_gross_reference_value_krw"]) == (
        Decimal("1.6217070") * 117800)
    assert row["actual_new_shares"] is None
    assert row["actual_cash_credit_krw"] is None
    assert row["realized_total_return_pct"] is None
    assert row["cash_rounding_and_actual_payment_verified"] is False


def test_cash_exchange_is_scheduled_not_paid():
    row = entitlements.hypothetical_entitlement(
        old_shares=10, consideration="7,000", successor_close_krw=None)
    assert row["scheduled_gross_cash_formula_krw"] == "70000"
    assert row["record_date_eligibility_verified"] is False
    assert row["actual_cash_credit_krw"] is None
    assert row["realized_total_return_pct"] is None


@pytest.mark.parametrize("shares,term,price", [
    (0, "0.5", 100), (True, "0.5", 100), (1.0, "0.5", 100),
    (1, 0.5, 100), (1, "NaN", 100), (1, "0", 100),
    (1, "1.2", 100), (1, "0.5", 0), (1, "0.5", True),
])
def test_invalid_or_ambiguous_inputs_fail_closed(shares, term, price):
    with pytest.raises(ValueError):
        entitlements.hypothetical_entitlement(
            old_shares=shares, consideration=term, successor_close_krw=price)


def test_company_wide_issued_share_count_is_not_a_holder_input():
    one = entitlements.hypothetical_entitlement(
        old_shares=1, consideration="0.1621707", successor_close_krw=117800)
    assert "company_issued_shares" not in one
    assert Decimal(one["unrounded_gross_reference_value_krw"]) == Decimal("19103.70846")


def test_fraction_reference_rule_must_be_in_archived_original(tmp_path):
    receipt = "20260102000224"
    case = SimpleNamespace(dart_archive=("source.zip", "unused"), receipt=receipt)

    def save(text):
        original = io.BytesIO()
        with ZipFile(original, "w") as inner:
            inner.writestr(f"{receipt}.xml", text)
        with ZipFile(tmp_path / "source.zip", "w") as outer:
            outer.writestr(f"raw/doc/{receipt}.zip", original.getvalue())

    save("<p>주권상장일에 한국거래소에서 거래되는 종가</p>")
    entitlements._assert_fraction_rule(tmp_path, case)
    save("<p>가격 기준이 명시되지 않음</p>")
    with pytest.raises(ValueError, match="does not confirm"):
        entitlements._assert_fraction_rule(tmp_path, case)
