"""Research settlement evidence, separate from decision-time signal inputs.

An empty verified event list is an explicit source claim, not an inference from
unchanged listed shares or a missing disclosure search result. Historical
backfills may settle retrospective returns but never become PIT signal inputs.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Literal

from signal_desk import market_clock


def _aware(value: dt.datetime | None) -> bool:
    return (isinstance(value, dt.datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass(frozen=True)
class SettlementEvent:
    kind: Literal["split", "cash_dividend", "halt", "delisting",
                  "treasury_cancellation", "option_exercise_issuance"]
    split_numerator: int | None = None
    split_denominator: int | None = None
    cash_per_share: float | None = None
    payment_date: str | None = None
    event_id: str | None = None
    cash_amount_status: Literal["scheduled", "final"] | None = None
    payment_status: Literal["scheduled", "confirmed"] | None = None
    payment_source_id: str | None = None
    payment_source_sha256: str | None = None
    payment_observed_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.kind == "split":
            if (not isinstance(self.split_numerator, int) or isinstance(self.split_numerator, bool)
                    or not isinstance(self.split_denominator, int)
                    or isinstance(self.split_denominator, bool)
                    or self.split_numerator < 1 or self.split_denominator < 1
                    or self.cash_per_share is not None or self.payment_date is not None
                    or self.event_id is not None or self.cash_amount_status is not None
                    or self.payment_status is not None or self.payment_source_id is not None
                    or self.payment_source_sha256 is not None
                    or self.payment_observed_at is not None):
                raise ValueError("invalid verified split terms")
        elif self.kind == "cash_dividend":
            if (self.split_numerator is not None or self.split_denominator is not None
                    or not isinstance(self.cash_per_share, (int, float))
                    or isinstance(self.cash_per_share, bool)
                    or not math.isfinite(self.cash_per_share) or self.cash_per_share < 0
                    or not self.payment_date):
                raise ValueError("invalid verified dividend terms")
            if ((self.event_id is not None and
                 (not isinstance(self.event_id, str) or not self.event_id.strip()))
                    or self.cash_amount_status not in (None, "scheduled", "final")
                    or self.payment_status not in (None, "scheduled", "confirmed")
                    or (self.payment_status == "confirmed"
                        and (self.cash_amount_status != "final"
                             or not isinstance(self.payment_source_id, str)
                             or not self.payment_source_id.strip()
                             or not isinstance(self.payment_source_sha256, str)
                             or not re.fullmatch(r"[0-9a-f]{64}", self.payment_source_sha256)
                             or not _aware(self.payment_observed_at)
                             or self.payment_observed_at.date()
                             < dt.date.fromisoformat(self.payment_date)))
                    or (self.payment_status != "confirmed"
                        and any(value is not None for value in (
                            self.payment_source_id, self.payment_source_sha256,
                            self.payment_observed_at)))):
                raise ValueError("invalid dividend evidence status")
            try:
                dt.date.fromisoformat(self.payment_date)
            except ValueError as exc:
                raise ValueError("invalid dividend payment date") from exc
        elif self.kind in ("halt", "delisting", "treasury_cancellation",
                           "option_exercise_issuance"):
            if any(value is not None for value in (
                    self.split_numerator, self.split_denominator,
                    self.cash_per_share, self.payment_date, self.event_id,
                    self.cash_amount_status, self.payment_status,
                    self.payment_source_id, self.payment_source_sha256,
                    self.payment_observed_at)):
                raise ValueError("non-proportional event has no settlement terms")
        else:
            raise ValueError("unknown settlement event")


@dataclass(frozen=True)
class DailyEventEvidence:
    market: Literal["kr", "us"]
    ticker: str
    session: str
    coverage: Literal["verified_complete", "unknown"]
    events: tuple[SettlementEvent, ...] = ()
    source_id: str | None = None
    source_published_at: dt.datetime | None = None
    source_observed_at: dt.datetime | None = None
    # Retrospective evidence is valid for settlement, never for the old signal.
    availability_kind: Literal["observed", "retrospective"] = "retrospective"

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not market_clock.is_session(self.market, self.session)
                or not self.ticker or self.coverage not in ("verified_complete", "unknown")
                or not isinstance(self.events, tuple)
                or any(not isinstance(event, SettlementEvent) for event in self.events)
                or self.availability_kind not in ("observed", "retrospective")):
            raise ValueError("invalid daily corporate-action evidence")
        if self.coverage == "unknown":
            if self.events:
                raise ValueError("unknown coverage cannot assert events")
            return
        if (not self.source_id or not _aware(self.source_published_at)
                or not _aware(self.source_observed_at)
                or self.source_observed_at < self.source_published_at):
            raise ValueError("verified coverage needs source and publication/observation times")
        if any(event.kind == "cash_dividend" and event.payment_date < self.session
               for event in self.events):
            raise ValueError("dividend payment precedes ex date")
        if sum(event.kind == "split" for event in self.events) > 1:
            raise ValueError("ambiguous same-day split sequence")
        dividend_ids = [event.event_id for event in self.events
                        if event.kind == "cash_dividend" and event.event_id is not None]
        if len(dividend_ids) != len(set(dividend_ids)):
            raise ValueError("duplicate dividend entitlement")
        if any(event.kind == "split" for event in self.events) and any(
                event.kind == "cash_dividend" for event in self.events):
            raise ValueError("split/dividend same-day share basis is unverified")
