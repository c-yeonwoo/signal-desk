"""Research-only share exchange valuation with explicit ratio precision.

No output from this module is a fill, an account credit, or an action-complete
strategy return. Callers must prove holding eligibility and action coverage
independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Literal

from signal_desk import market_clock


Precision = Literal["exact_contractual_decimal", "derived_exact_rational",
                    "displayed_with_documented_rounding", "displayed_precision_unknown"]


def _number(value: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("decimal terms must be strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal term") from exc
    if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
        raise ValueError("decimal term must be finite and nonnegative")
    return result


def _display(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class RatioEvidence:
    declared_delivery_ratio: str
    precision: Precision
    source_id: str
    # Bounds must come from an independently documented display rule, not a
    # guess derived from the number of printed decimal places.
    lower: str | None = None
    upper: str | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    rounding_rule_source_id: str | None = None

    def __post_init__(self) -> None:
        _number(self.declared_delivery_ratio)
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("ratio source is required")
        if self.precision not in ("exact_contractual_decimal", "derived_exact_rational",
                                  "displayed_with_documented_rounding", "displayed_precision_unknown"):
            raise ValueError("unknown ratio precision")
        if not isinstance(self.lower_inclusive, bool) or not isinstance(self.upper_inclusive, bool):
            raise ValueError("interval closure must be explicit")
        if self.precision == "displayed_with_documented_rounding":
            if not all((self.lower, self.upper, self.rounding_rule_source_id)):
                raise ValueError("documented interval needs bounds and rule source")
            if _number(self.lower) >= _number(self.upper):
                raise ValueError("ratio interval is empty or reversed")
            if not (_number(self.lower) <= _number(self.declared_delivery_ratio)
                    <= _number(self.upper)):
                raise ValueError("declared ratio lies outside documented interval")
        elif any((self.lower is not None, self.upper is not None,
                  self.rounding_rule_source_id is not None)):
            raise ValueError("undocumented ratio bounds are forbidden")


@dataclass(frozen=True)
class ShareExchangeTerms:
    """Reviewed simulation terms, never evidence of a personal account credit."""

    event_id: str
    old_ticker: str
    old_security_id: str
    successor_ticker: str
    successor_security_id: str
    effective_session: str
    first_tradable_session: str
    ratio: RatioEvidence
    source_id: str

    def __post_init__(self) -> None:
        if (not all(isinstance(value, str) and value.strip() for value in (
                self.event_id, self.old_ticker, self.old_security_id,
                self.successor_ticker, self.successor_security_id, self.source_id))
                or self.old_ticker == self.successor_ticker
                or self.old_security_id == self.successor_security_id
                or not market_clock.is_session("kr", self.effective_session)
                or not market_clock.is_session("kr", self.first_tradable_session)
                or self.first_tradable_session < self.effective_session
                or not isinstance(self.ratio, RatioEvidence)):
            raise ValueError("invalid share exchange terms")


def _integer(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _value(n: int, ratio: Decimal, listing: Decimal, mark: Decimal,
           *, left_limit: bool = False) -> tuple[int, Decimal, Decimal]:
    shares = n * ratio
    whole = _integer(shares)
    if left_limit and shares == whole:
        whole -= 1
    fraction = shares - whole
    return whole, fraction, whole * mark + fraction * listing


def _boundary_value(shares: int, listing: Decimal, mark: Decimal,
                    *, left_limit: bool) -> Decimal:
    # A repeating rational k/n cannot safely be converted to Decimal and then
    # multiplied back by n: the rounded result might fall below k.
    return (shares - 1) * mark + listing if left_limit else shares * mark


def value_exchange(*, old_shares: int, ratio: RatioEvidence,
                   fractional_listing_price_krw: str,
                   successor_mark_price_krw: str) -> dict:
    """Value coupled shares and fractional cash under stated, unrounded terms.

    For an interval, extrema can occur at an interior integer-share boundary.
    Prices are independent inputs: the first listing close prices the fraction;
    the successor's mark price values whole shares on the valuation date.
    """
    if isinstance(old_shares, bool) or not isinstance(old_shares, int) or not 1 <= old_shares <= 1_000_000_000:
        raise ValueError("old shares must be a positive integer")
    if not isinstance(ratio, RatioEvidence):
        raise ValueError("ratio evidence is required")
    with localcontext() as ctx:
        ctx.prec = 60
        listing = _number(fractional_listing_price_krw)
        mark = _number(successor_mark_price_krw)
        reported = _number(ratio.declared_delivery_ratio)
        q, fraction, amount = _value(old_shares, reported, listing, mark)
        base = {"research_only": True, "live_order_eligible": False,
                "registered_verdict": False, "account_realized_return_pct": None,
                "ratio_source_id": ratio.source_id,
                "declared_delivery_ratio": ratio.declared_delivery_ratio,
                "ratio_precision": ratio.precision,
                "reported_terms_scenario": {
                    "whole_new_shares": q, "fractional_new_share": _display(fraction),
                    "unrounded_fractional_cash_krw": _display(fraction * listing),
                    "unrounded_gross_entitlement_krw": _display(amount),
                    "successor_mark_price_krw": _display(mark),
                    "fractional_listing_price_krw": _display(listing),
                    "rounded_cash_krw": None}}
        if ratio.precision == "displayed_precision_unknown":
            return {**base, "exact_value": None, "documented_interval": None,
                    "valuation_state": "reported_terms_scenario_only"}
        if ratio.precision != "displayed_with_documented_rounding":
            return {**base, "exact_value": base["reported_terms_scenario"],
                    "documented_interval": None,
                    "valuation_state": "exact_ratio_unrounded_cash"}

        low, high = _number(ratio.lower), _number(ratio.upper)
        candidates: list[tuple[Decimal, bool]] = []
        candidates.append((_value(old_shares, low, listing, mark)[2], ratio.lower_inclusive))
        # At an excluded upper integer boundary, only the left limit exists.
        high_integer = old_shares * high == _integer(old_shares * high)
        if ratio.upper_inclusive:
            candidates.append((_value(old_shares, high, listing, mark)[2], True))
        elif not high_integer:
            candidates.append((_value(old_shares, high, listing, mark)[2], False))

        low_shares, high_shares = old_shares * low, old_shares * high
        first = int(low_shares.to_integral_value(rounding=ROUND_CEILING))
        last = _integer(high_shares)
        # The earliest integer can be the minimum following a downward jump.
        if first <= last and (first < high_shares or
                              ratio.upper_inclusive and first == high_shares):
            candidates.append((_boundary_value(first, listing, mark, left_limit=False),
                               first > low_shares or ratio.lower_inclusive))
        # The last integer can be the supremum immediately before a jump.
        if last >= first and last > low_shares:
            candidates.append((_boundary_value(last, listing, mark,
                                               left_limit=True), False))
        if not ratio.upper_inclusive and high_integer:
            candidates.append((_boundary_value(last, listing, mark,
                                               left_limit=True), False))
        minimum = min(value for value, _ in candidates)
        maximum = max(value for value, _ in candidates)
        return {**base, "exact_value": None,
                "documented_interval": {
                    "lower_unrounded_gross_krw": _display(minimum),
                    "upper_unrounded_gross_krw": _display(maximum),
                    "lower_attained": any(value == minimum and attained for value, attained in candidates),
                    "upper_attained": any(value == maximum and attained for value, attained in candidates),
                    "rounding_rule_source_id": ratio.rounding_rule_source_id,
                    "cash_rounding_verified": False},
                "valuation_state": "documented_ratio_interval_unrounded_cash"}
