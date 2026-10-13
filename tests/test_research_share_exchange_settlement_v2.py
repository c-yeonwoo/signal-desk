"""Boundary cases that cannot be inferred from two printed ratio strings."""

from decimal import Decimal

import pytest

from scripts.measure.research_share_exchange_settlement_v2 import review
from signal_desk.signals.research_share_exchange_settlement import RatioEvidence, value_exchange


def test_displayed_ratio_has_scenario_but_no_certified_interval():
    ratio = RatioEvidence("0.1621707", "displayed_precision_unknown", "DART:20260102000224")
    result = value_exchange(old_shares=100, ratio=ratio,
                            fractional_listing_price_krw="117800",
                            successor_mark_price_krw="90000")
    assert result["reported_terms_scenario"]["whole_new_shares"] == 16
    assert result["exact_value"] is None
    assert result["documented_interval"] is None
    assert result["account_realized_return_pct"] is None
    with pytest.raises(ValueError, match="undocumented"):
        RatioEvidence("0.1621707", "displayed_precision_unknown", "source",
                      lower="0.16217065", upper="0.16217075")


def test_documented_interval_extrema_cross_an_integer_share_boundary():
    ratio = RatioEvidence("0.1", "displayed_with_documented_rounding", "terms",
                          lower="0.09999", upper="0.10001",
                          rounding_rule_source_id="official-rule")
    result = value_exchange(old_shares=100, ratio=ratio,
                            fractional_listing_price_krw="100",
                            successor_mark_price_krw="50")
    bounds = result["documented_interval"]
    assert Decimal(bounds["lower_unrounded_gross_krw"]) == 500
    assert bounds["lower_attained"] is True
    assert Decimal(bounds["upper_unrounded_gross_krw"]) == 550
    assert bounds["upper_attained"] is False
    assert result["exact_value"] is None


def test_repeating_boundary_does_not_round_to_wrong_side():
    ratio = RatioEvidence("0.3333333333333333333333333333333333333333",
                          "displayed_with_documented_rounding", "terms",
                          lower="0.3333333333333333333333333333333333333332",
                          upper="0.3333333333333333333333333333333333333334",
                          rounding_rule_source_id="official-rule")
    bounds = value_exchange(old_shares=3, ratio=ratio,
                            fractional_listing_price_krw="100",
                            successor_mark_price_krw="50")["documented_interval"]
    assert Decimal(bounds["lower_unrounded_gross_krw"]) == 50
    assert Decimal(bounds["upper_unrounded_gross_krw"]) == 100
    assert bounds["upper_attained"] is False


def test_exact_contractual_decimal_has_coupled_whole_and_fraction():
    result = value_exchange(old_shares=10,
                            ratio=RatioEvidence("0.1621707", "exact_contractual_decimal", "contract"),
                            fractional_listing_price_krw="117800",
                            successor_mark_price_krw="90000")
    exact = result["exact_value"]
    assert exact["whole_new_shares"] == 1
    assert Decimal(exact["fractional_new_share"]) == Decimal("0.6217070")
    assert Decimal(exact["unrounded_gross_entitlement_krw"]) == (
        Decimal(90000) + Decimal("0.6217070") * Decimal(117800))
    assert exact["rounded_cash_krw"] is None


def test_frozen_case_report_keeps_account_and_strategy_returns_unavailable():
    report = review(old_shares=100)
    assert set(report["cases"]) == {"010620", "042670"}
    assert report["whole_window_total_return_pct"] is None
    for case in report["cases"].values():
        assert case["simulated_entitlement_status"] == "conditional_on_hypothetical_record_holder"
        assert case["economic_valuation_status"] == "reported_terms_scenario_only"
        assert case["valuation"]["documented_interval"] is None
        assert case["account_realized_status"] == "account_evidence_unavailable"
        assert case["missing_by_purpose"]["account_realized"] == [
            "actual account share delivery and cash credit"]


@pytest.mark.parametrize("shares,price", [(0, "100"), (True, "100"), (1, "NaN"), (1, "0")])
def test_invalid_inputs_fail_closed(shares, price):
    with pytest.raises(ValueError):
        value_exchange(old_shares=shares,
                       ratio=RatioEvidence("0.1", "exact_contractual_decimal", "contract"),
                       fractional_listing_price_krw=price, successor_mark_price_krw="50")
