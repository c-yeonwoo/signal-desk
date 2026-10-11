"""Research settlement evidence, separate from decision-time signal inputs.

An empty verified event list is an explicit source claim, not an inference from
unchanged listed shares or a missing disclosure search result. Historical
backfills may settle retrospective returns but never become PIT signal inputs.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

from signal_desk import market_clock


def _aware(value: dt.datetime | None) -> bool:
    return (isinstance(value, dt.datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass(frozen=True)
class SettlementEvent:
    kind: Literal["split", "cash_dividend", "halt", "delisting"]
    split_numerator: int | None = None
    split_denominator: int | None = None
    cash_per_share: float | None = None
    payment_date: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "split":
            if (not isinstance(self.split_numerator, int) or isinstance(self.split_numerator, bool)
                    or not isinstance(self.split_denominator, int)
                    or isinstance(self.split_denominator, bool)
                    or self.split_numerator < 1 or self.split_denominator < 1
                    or self.cash_per_share is not None or self.payment_date is not None):
                raise ValueError("invalid verified split terms")
        elif self.kind == "cash_dividend":
            if (self.split_numerator is not None or self.split_denominator is not None
                    or not isinstance(self.cash_per_share, (int, float))
                    or isinstance(self.cash_per_share, bool)
                    or not math.isfinite(self.cash_per_share) or self.cash_per_share < 0
                    or not self.payment_date):
                raise ValueError("invalid verified dividend terms")
            try:
                dt.date.fromisoformat(self.payment_date)
            except ValueError as exc:
                raise ValueError("invalid dividend payment date") from exc
        elif self.kind in ("halt", "delisting"):
            if any(value is not None for value in (
                    self.split_numerator, self.split_denominator,
                    self.cash_per_share, self.payment_date)):
                raise ValueError("halt or delisting has no settlement terms")
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
        if any(event.kind == "split" for event in self.events) and any(
                event.kind == "cash_dividend" for event in self.events):
            raise ValueError("split/dividend same-day share basis is unverified")
