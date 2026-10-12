"""A source response must not silently become an action-free trading day."""

import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from signal_desk.signals.research_action_coverage import (
    ActionRecord, AvailabilityEvidence, CoverageCertificate, OfficialKindScope,
    SourceWindow,
)

OBSERVED = dt.datetime(2026, 10, 11, 17, 30, tzinfo=ZoneInfo("Asia/Seoul"))
HASH = "a" * 64


def _window(family="KSD", basis="effective", kinds=frozenset({"cash_dividend"}),
            state="complete_for_source"):
    return SourceWindow("kr", "KR7005930003", family, family, "2024-04-01", "2024-04-30",
                        basis, kinds, state, (HASH,), OBSERVED, True)


def _cert(*windows, unresolved=()):
    return CoverageCertificate("kr", "KR7005930003", "2024-04-01", "2024-04-30",
                               frozenset({"cash_dividend", "split"}), tuple(windows),
                               HASH, tuple(unresolved))


def test_scope_needs_effective_date_all_types_independent_family_and_reconciliation():
    registry = _window(kinds=frozenset({"cash_dividend", "split"}))
    filing = _window(family="DART", kinds=frozenset({"cash_dividend"}))
    assert _cert(registry, filing).state == "partial"
    filing_all = replace(filing, event_kinds=frozenset({"cash_dividend", "split"}))
    assert _cert(registry, filing_all).state == "complete_for_scope"
    assert _cert(registry, replace(filing, upstream_family="KSD")).state == "partial"
    assert _cert(registry, replace(filing, date_basis="receipt")).state == "partial"
    assert _cert(_window(), filing).state == "partial"
    assert _cert(registry, filing_all, unresolved=("unknown-merger",)).state == "partial"
    assert replace(_cert(registry, filing_all), reconciliation_sha256=None).state == "partial"
    assert _cert(replace(registry, historical_range_confirmed=False), filing).state == "partial"
    assert _cert(replace(registry, state="source_error"), filing).state == "source_error"
    assert _cert().state == "unknown"


def test_kind_specific_adjacent_sources_and_official_scope():
    first = replace(_window(kinds=frozenset({"cash_dividend", "split"})),
                    end="2024-04-15")
    second = replace(first, start="2024-04-16", end="2024-04-30")
    independent = _window(family="DART", kinds=frozenset({"cash_dividend", "split"}))
    assert _cert(first, second, independent).state == "complete_for_scope"
    assert _cert(first, replace(second, start="2024-04-17"), independent).state == "partial"
    unrelated_error = replace(independent, market="us", state="source_error")
    assert _cert(first, second, independent, unrelated_error).state == "complete_for_scope"

    official = OfficialKindScope("split", _window(family="KRX_CA", kinds=frozenset({"split"})),
                                 HASH, HASH, HASH)
    only_dividend = _cert(_window(), _window(family="DART"))
    assert replace(only_dividend, official_scopes=(official,)).state == "partial"
    bound = replace(only_dividend, windows=(*only_dividend.windows, official.window))
    assert replace(bound, official_scopes=(official,)).state == "complete_for_scope"
    assert replace(bound, official_scopes=(
        replace(official, unresolved_event_ids=("unresolved-split",)),)).state == "partial"
    assert replace(bound, official_scopes=(
        replace(official, window=replace(official.window, date_basis="receipt")),)).state == "partial"


def test_source_empty_and_partial_records_do_not_forge_complete_scope():
    receipt_empty = _window(basis="receipt", state="source_empty")
    assert _cert(receipt_empty).state == "partial"
    with pytest.raises(ValueError, match="saved raw"):
        replace(receipt_empty, page_hashes=())
    with pytest.raises(ValueError, match="invalid source window"):
        replace(receipt_empty, page_hashes=("bad",))


def test_late_dividend_amount_cannot_become_march_decision_input():
    publication = AvailabilityEvidence("2024-04-30", OBSERVED,
                                       availability_kind="retrospective")
    row = ActionRecord("kr", "KR7005930003", "20240430800061", "cash_dividend",
                       "2024-03-28", "scheduled", "DART:20240430800061", HASH,
                       publication, share_class="common", currency="KRW",
                       cash_per_share=361, record_date="2024-03-31",
                       scheduled_payment_date="2024-05-20")
    assert row.status == "scheduled" and row.actual_payment_date is None
    march = dt.datetime(2024, 3, 29, 16, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    assert publication.available_to_daily_decision("kr", march) is False
    with pytest.raises(ValueError, match="paid dividend needs"):
        replace(row, status="paid")

    receipt = dt.datetime(2024, 4, 30, 17, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    observed = AvailabilityEvidence("2024-04-30", receipt,
                                    availability_kind="observed")
    assert observed.available_to_daily_decision("kr", receipt) is False
    next_session = dt.datetime(2024, 5, 2, 16, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    assert observed.available_to_daily_decision("kr", next_session) is True


def test_split_needs_effective_ratio_but_announcement_can_remain_partial():
    availability = AvailabilityEvidence("2024-04-01", OBSERVED)
    record = ActionRecord("kr", "KR7005930003", "event", "split", None,
                          "announced", "issuer", HASH, availability)
    assert record.effective_session is None
    with pytest.raises(ValueError, match="positive share ratio"):
        replace(record, status="effective", effective_session="2024-04-02")


def test_date_only_publication_uses_local_next_session_even_after_weekend():
    observed = dt.datetime(2024, 4, 27, 10, tzinfo=ZoneInfo("Asia/Seoul"))
    evidence = AvailabilityEvidence("2024-04-27", observed, availability_kind="observed")
    sunday = dt.datetime(2024, 4, 28, 23, tzinfo=dt.timezone.utc)
    monday = dt.datetime(2024, 4, 29, 1, tzinfo=dt.timezone.utc)
    assert evidence.available_to_daily_decision("kr", sunday) is True  # Monday in Seoul
    assert evidence.available_to_daily_decision("kr", monday) is True
    assert evidence.available_to_daily_decision("us", sunday) is False  # Sunday in New York
    assert evidence.available_to_daily_decision("bad", monday) is False
