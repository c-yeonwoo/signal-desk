"""Macro source provenance and fail-closed transport without changing votes."""

import json
import logging

from signal_desk import config
from signal_desk.ingest import ecos, fred


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, limit):
        return self.body[:limit]


def test_fred_error_does_not_log_api_key(monkeypatch, caplog):
    monkeypatch.setattr(config, "fred_key", lambda: "private-test-token")

    def fail(url):
        raise RuntimeError(url)

    monkeypatch.setattr(fred, "_open", fail)
    with caplog.at_level(logging.ERROR):
        assert fred._observations("DGS10", 3) == []
    assert "private-test-token" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_macro_keyed_requests_do_not_follow_redirects(monkeypatch):
    calls = []

    def fake_opener(handler):
        assert handler.redirect_request(None, None, 302, "moved", {}, "https://elsewhere.example/") is None

        class Opener:
            def open(self, url, timeout):
                calls.append((url, timeout))
                return "response"

        return Opener()

    monkeypatch.setattr(fred.urllib.request, "build_opener", fake_opener)
    assert fred._open("https://api.stlouisfed.org/test") == "response"
    assert ecos._open("https://ecos.bok.or.kr/test") == "response"
    assert len(calls) == 2


def test_fred_invalid_or_oversized_values_do_not_become_market_votes(monkeypatch):
    monkeypatch.setattr(config, "fred_key", lambda: "private-test-token")
    body = {"observations": [
        {"date": "2026-10-02", "value": "4.1"},
        {"date": "2026-10-01", "value": "NaN"},
        {"date": "2026-10-32", "value": "4.2"},
        {"date": "2026-09-30", "value": "."},
    ]}
    monkeypatch.setattr(fred, "_open", lambda *_a: _Response(json.dumps(body).encode()))
    assert fred._observations("DGS10", 4) == [("2026-10-02", 4.1)]

    monkeypatch.setattr(fred, "_open", lambda *_a: _Response(b"x" * (fred._MAX_RESPONSE_BYTES + 1)))
    assert fred._observations("DGS10", 4) == []


def test_fred_provenance_discloses_unverified_publication_time(monkeypatch):
    monkeypatch.setattr(fred, "_observations", lambda *_: [("2026-10-02", 4.1), ("2026-10-01", 4.0)])
    row = next(item for item in fred.macro_indicators() if item["key"] == "DGS10")
    assert row["source_url"] == "https://fred.stlouisfed.org/series/DGS10"
    assert row["source_published_at"] is None
    assert row["strict_pit_eligible"] is False
    assert row["change"] == 0.1


def test_ecos_filters_bad_observations_and_marks_provenance(monkeypatch):
    monkeypatch.setattr(config, "ecos_key", lambda: "private-test-token")
    body = {"StatisticSearch": {"row": [
        {"TIME": "202609", "DATA_VALUE": "2.5"},
        {"TIME": "202610", "DATA_VALUE": "Infinity"},
        {"TIME": "202613", "DATA_VALUE": "2.7"},
    ]}}
    monkeypatch.setattr(ecos, "_open", lambda *_a: _Response(json.dumps(body).encode()))
    assert ecos._series("722Y001", "M", "0101000", 3) == [("202609", 2.5)]

    monkeypatch.setattr(ecos, "_series", lambda *_: [("202609", 2.5), ("202608", 2.5)])
    row = next(item for item in ecos.macro_indicators() if item["key"] == "KR_BASE")
    assert row["source_series"] == "722Y001/M/0101000"
    assert row["source_published_at"] is None and row["strict_pit_eligible"] is False
    assert row["favor"] == 0


def test_cpi_invalid_denominator_is_not_a_vote(monkeypatch):
    monkeypatch.setattr(fred, "_observations", lambda series, _limit:
                        [(f"2025-{month:02d}-01", 0.0 if month == 1 else 100.0)
                         for month in range(12, 0, -1)] + [("2024-12-01", 0.0)]
                        if series == "CPIAUCSL" else [])
    assert all(row["key"] != "CPIAUCSL" for row in fred.macro_indicators())

    monkeypatch.setattr(ecos, "_series", lambda code, *_:
                        [("202612", 100.0)] * 12 + [("202512", 0.0)]
                        if code == "901Y009" else [])
    assert all(row["key"] != "KR_CPI" for row in ecos.macro_indicators())
