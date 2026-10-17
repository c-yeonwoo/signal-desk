"""Bounded historical corporate-action evidence; never a live-order input.

Fetching a source is not proof that a security had no other action. A complete
scope requires an effective-date catalogue, independent reconciliation and no
unresolved records. Raw receipts may remain useful while the scope is partial.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from signal_desk import market_clock

ActionKind = Literal["cash_dividend", "split", "halt", "delisting", "unsupported",
                     "stock_dividend", "rights", "capital_reduction", "spin_off",
                     "merger_exchange", "symbol_change", "share_count_change"]
CollectionState = Literal["complete_for_source", "source_empty", "partial", "source_error"]
_SHA = re.compile(r"[0-9a-f]{64}\Z")
ACTION_KINDS = frozenset({"cash_dividend", "split", "halt", "delisting",
                          "unsupported", "stock_dividend", "rights",
                          "capital_reduction", "spin_off", "merger_exchange",
                          "symbol_change", "share_count_change"})


@dataclass(frozen=True)
class ReviewedShareCountChange:
    """One issuer-share fact; it cannot assert an action-free holding window."""

    ticker: str
    security_id: str
    share_class: Literal["ordinary_common"]
    kind: Literal["treasury_cancellation", "option_exercise_issuance", "convertible_bond_issuance"]
    effective_on: str
    listing_session: str
    previous_session: str
    listed_shares_before: int
    listed_shares_after: int
    source_url: str
    source_sha256: str
    source_filing_date: str
    source_observed_at: dt.datetime
    source_span: str
    holder_quantity_effect: Literal["unchanged"] = "unchanged"
    coverage_state: Literal["partial"] = "partial"

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[0-9A-Z]{6}", self.ticker)
                or not re.fullmatch(r"KR[0-9A-Z]{10}", self.security_id)
                or self.share_class != "ordinary_common"
                or self.kind not in ("treasury_cancellation", "option_exercise_issuance", "convertible_bond_issuance")
                or not market_clock.is_session("kr", self.listing_session)
                or not market_clock.is_session("kr", self.previous_session)
                or self.previous_session >= self.listing_session
                or _date(self.effective_on) > _date(self.listing_session)
                or _date(self.source_filing_date) > _date(self.listing_session)
                or not _aware(self.source_observed_at)
                or self.source_filing_date > self.source_observed_at.date().isoformat()
                or not isinstance(self.listed_shares_before, int)
                or isinstance(self.listed_shares_before, bool)
                or not isinstance(self.listed_shares_after, int)
                or isinstance(self.listed_shares_after, bool)
                or self.listed_shares_before <= 0 or self.listed_shares_after <= 0
                or ((self.listed_shares_after < self.listed_shares_before)
                    != (self.kind == "treasury_cancellation"))
                or not self.source_url.startswith("https://kind.krx.co.kr/external/")
                or not _SHA.fullmatch(self.source_sha256)
                or not self.source_span.strip()
                or self.holder_quantity_effect != "unchanged"
                or self.coverage_state != "partial"):
            raise ValueError("invalid reviewed share-count change")


def _date(value: str) -> dt.date:
    if not isinstance(value, str) or dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be ISO YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def _aware(value: dt.datetime) -> bool:
    return isinstance(value, dt.datetime) and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class SourceWindow:
    market: Literal["kr", "us"]
    security_id: str
    source_id: str
    upstream_family: str
    start: str
    end: str
    date_basis: Literal["receipt", "record", "effective", "unknown"]
    event_kinds: frozenset[ActionKind]
    state: CollectionState
    page_hashes: tuple[str, ...]
    observed_at: dt.datetime
    # A response may be successful and still lack a known historical retention bound.
    historical_range_confirmed: bool = False

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not self.security_id or not self.source_id
                or not self.upstream_family or _date(self.start) > _date(self.end)
                or self.date_basis not in ("receipt", "record", "effective", "unknown")
                or not self.event_kinds or not self.event_kinds <= ACTION_KINDS
                or self.state not in ("complete_for_source", "source_empty", "partial", "source_error")
                or not isinstance(self.page_hashes, tuple)
                or any(not _SHA.fullmatch(value) for value in self.page_hashes)
                or not _aware(self.observed_at)):
            raise ValueError("invalid source window")
        if self.state in ("complete_for_source", "source_empty") and not self.page_hashes:
            raise ValueError("successful source collection needs saved raw response hashes")


@dataclass(frozen=True)
class AvailabilityEvidence:
    """A publication date without a time is not an intraday timestamp."""

    published_on: str | None
    observed_at: dt.datetime
    published_at: dt.datetime | None = None
    availability_kind: Literal["observed", "retrospective"] = "retrospective"

    def __post_init__(self) -> None:
        if self.published_on is not None:
            _date(self.published_on)
        if (not _aware(self.observed_at)
                or (self.published_at is not None and
                    (self.published_on is None or not _aware(self.published_at)
                     or self.published_at.date().isoformat() != self.published_on
                     or self.published_at > self.observed_at))
                or self.availability_kind not in ("observed", "retrospective")):
            raise ValueError("invalid publication/observation evidence")

    def available_to_daily_decision(self, market: str, as_of: dt.datetime) -> bool:
        if (market not in ("kr", "us") or self.published_on is None
                or self.availability_kind != "observed"
                or not _aware(as_of) or self.observed_at > as_of):
            return False
        if self.published_at is not None:
            return self.published_at <= as_of
        zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
        local_day = as_of.astimezone(zone).date()
        published_day = _date(self.published_on)
        # A date-only release is usable no sooner than the next *market*
        # session, including when it was published on a weekend or holiday.
        return any(market_clock.is_session(market, published_day + dt.timedelta(days=offset))
                   for offset in range(1, min((local_day - published_day).days, 30) + 1))


@dataclass(frozen=True)
class ActionRecord:
    market: Literal["kr", "us"]
    security_id: str
    event_id: str
    kind: ActionKind
    effective_session: str | None
    status: Literal["announced", "scheduled", "effective", "paid", "cancelled", "unresolved"]
    source_id: str
    raw_sha256: str
    availability: AvailabilityEvidence
    issuer_name: str | None = None
    share_class: str | None = None
    currency: str | None = None
    cash_per_share: float | None = None
    split_numerator: int | None = None
    split_denominator: int | None = None
    record_date: str | None = None
    listing_session: str | None = None
    scheduled_payment_date: str | None = None
    actual_payment_date: str | None = None
    correction_of: str | None = None
    source_span: str | None = None

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not self.security_id or not self.event_id
                or self.kind not in ACTION_KINDS
                or self.status not in ("announced", "scheduled", "effective", "paid", "cancelled", "unresolved")
                or not self.source_id or not _SHA.fullmatch(self.raw_sha256)
                or not isinstance(self.availability, AvailabilityEvidence)
                or (self.issuer_name is not None and not self.issuer_name.strip())
                or (self.effective_session is not None and
                    not market_clock.is_session(self.market, self.effective_session))
                or (self.record_date is not None and not _date(self.record_date))
                or (self.listing_session is not None and
                    not market_clock.is_session(self.market, self.listing_session))
                or (self.scheduled_payment_date is not None and not _date(self.scheduled_payment_date))
                or (self.actual_payment_date is not None and not _date(self.actual_payment_date))):
            raise ValueError("invalid corporate-action record")
        if self.kind == "cash_dividend":
            if (self.cash_per_share is not None and
                    (isinstance(self.cash_per_share, bool)
                     or not math.isfinite(self.cash_per_share) or self.cash_per_share < 0)):
                raise ValueError("invalid dividend amount")
            if self.status == "paid" and (self.cash_per_share is None or not self.currency
                                          or not self.actual_payment_date or not self.share_class):
                raise ValueError("paid dividend needs confirmed cash, class, currency and date")
        elif self.cash_per_share is not None or self.actual_payment_date is not None:
            raise ValueError("non-dividend cannot carry dividend cash")
        if self.kind == "split":
            ratio_missing = self.split_numerator is None or self.split_denominator is None
            ratio_invalid = (not ratio_missing and (
                not isinstance(self.split_numerator, int) or isinstance(self.split_numerator, bool)
                or not isinstance(self.split_denominator, int)
                or isinstance(self.split_denominator, bool)
                or self.split_numerator < 1 or self.split_denominator < 1))
            if ratio_invalid or (self.status == "effective" and ratio_missing):
                raise ValueError("effective split needs positive share ratio")
        elif self.split_numerator is not None or self.split_denominator is not None:
            raise ValueError("non-split cannot carry share ratio")


@dataclass(frozen=True)
class OfficialKindScope:
    """Reviewed assertion that one official catalogue enumerates one kind."""

    kind: ActionKind
    window: SourceWindow
    source_scope_sha256: str
    corrections_review_sha256: str
    event_reconciliation_sha256: str
    unresolved_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.kind not in ACTION_KINDS or not isinstance(self.window, SourceWindow)
                or any(not _SHA.fullmatch(value) for value in (
                    self.source_scope_sha256, self.corrections_review_sha256,
                    self.event_reconciliation_sha256))
                or len(set(self.unresolved_event_ids)) != len(self.unresolved_event_ids)):
            raise ValueError("invalid official kind scope")


def _covers_range(windows: list[SourceWindow], start: str, end: str) -> bool:
    """An effective-date catalogue may arrive in adjacent bounded pages."""
    cursor = _date(start)
    last = _date(end)
    for window in sorted(windows, key=lambda item: item.start):
        first = _date(window.start)
        if first > cursor:
            return False
        cursor = max(cursor, _date(window.end) + dt.timedelta(days=1))
        if cursor > last:
            return True
    return False


@dataclass(frozen=True)
class CoverageCertificate:
    """A reviewed *scope*, not an assertion of universal provider completeness."""

    market: Literal["kr", "us"]
    security_id: str
    start: str
    end: str
    required_kinds: frozenset[ActionKind]
    windows: tuple[SourceWindow, ...]
    reconciliation_sha256: str | None = None
    unresolved_event_ids: tuple[str, ...] = ()
    official_scopes: tuple[OfficialKindScope, ...] = ()

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not self.security_id
                or _date(self.start) > _date(self.end)
                or not self.required_kinds or not self.required_kinds <= ACTION_KINDS
                or not isinstance(self.windows, tuple)
                or any(not isinstance(row, SourceWindow) for row in self.windows)
                or not isinstance(self.official_scopes, tuple)
                or any(not isinstance(row, OfficialKindScope) for row in self.official_scopes)
                or (self.reconciliation_sha256 is not None and
                    not _SHA.fullmatch(self.reconciliation_sha256))
                or len(set(self.unresolved_event_ids)) != len(self.unresolved_event_ids)):
            raise ValueError("invalid coverage certificate")

    @property
    def state(self) -> Literal["complete_for_scope", "partial", "source_error", "unknown"]:
        relevant = [row for row in self.windows if row.market == self.market
                    and row.security_id == self.security_id
                    and row.start <= self.end and row.end >= self.start
                    and bool(row.event_kinds & self.required_kinds)]
        if any(row.state == "source_error" for row in relevant):
            return "source_error"
        if not relevant and not self.official_scopes:
            return "unknown"
        eligible = [row for row in relevant if (
            row.date_basis == "effective" and row.historical_range_confirmed
            and row.state in ("complete_for_source", "source_empty"))]
        covered = set()
        for kind in self.required_kinds:
            by_family = {family: [row for row in eligible
                                  if row.upstream_family == family and kind in row.event_kinds]
                         for family in {row.upstream_family for row in eligible}}
            if sum(_covers_range(rows, self.start, self.end)
                   for rows in by_family.values()) >= 2:
                covered.add(kind)
            for reviewed in self.official_scopes:
                row = reviewed.window
                if (reviewed.kind == kind and not reviewed.unresolved_event_ids
                        and row in self.windows
                        and row.market == self.market and row.security_id == self.security_id
                        and row.date_basis == "effective" and row.historical_range_confirmed
                        and row.state in ("complete_for_source", "source_empty")
                        and kind in row.event_kinds
                        and _covers_range([row], self.start, self.end)):
                    covered.add(kind)
        if (self.reconciliation_sha256 and not self.unresolved_event_ids
                and self.required_kinds <= covered):
            return "complete_for_scope"
        return "partial"
