"""US watched-company facts require same tag, unit, period, and observed CIK."""

import json
import datetime as dt

from signal_desk.ingest import financial_evidence as evidence
from signal_desk.signals import financial_change


NOW = "2026-10-03T12:00:00+00:00"
CIK = "0000320193"


def _fact(value, start, end, filed, accession, *, form="10-Q"):
    return {"val": value, "start": start, "end": end, "filed": filed,
            "form": form, "accn": accession}


def _body(*, revenue_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
          prior_tag=None, amend=False):
    old = _fact(100, "2025-04-01", "2025-06-30", "2025-08-01", "0000320193-25-000001")
    new = _fact(120, "2026-04-01", "2026-06-30", "2026-08-01", "0000320193-26-000001")
    revenues = {revenue_tag: {"units": {"USD": [old, new]}}}
    if prior_tag:
        revenues = {revenue_tag: {"units": {"USD": [new]}},
                    prior_tag: {"units": {"USD": [old]}}}
    elif amend:
        revenues[revenue_tag]["units"]["USD"].append(
            _fact(110, "2026-04-01", "2026-06-30", "2026-09-01", "0000320193-26-000002", form="10-Q/A"))
    revenues["OperatingIncomeLoss"] = {"units": {"USD": [
        _fact(10, "2025-04-01", "2025-06-30", "2025-08-01", "0000320193-25-000001"),
        _fact(18, "2026-04-01", "2026-06-30", "2026-08-01", "0000320193-26-000001")]}}
    return {"cik": 320193, "facts": {"us-gaap": revenues}}


def _save(path, body, monkeypatch):
    monkeypatch.setattr(evidence, "_now", lambda: NOW)
    evidence.archive(path, evidence.Target("sec", CIK), json.dumps(body).encode(), observed_at=NOW)


def test_same_sec_tag_unit_quarter_and_source(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    _save(path, _body(), monkeypatch)
    item = financial_change.describe_sec(path, ticker="AAPL", issuer=CIK, as_of=NOW)
    assert item["status"] == "comparison" and item["market"] == "us"
    assert item["metrics"]["revenue"]["change_pct"] == 20.0
    assert item["metrics"]["operating_income"]["change_pct"] == 80.0
    assert item["metrics"]["revenue"]["current_source"].startswith("https://www.sec.gov/Archives/")
    assert item["not_order_advice"] and not item["source_available_at_verified"]
    assert financial_change.describe_sec(path, ticker="AAPL", issuer=CIK,
                                         as_of="2026-10-02T00:00:00Z")["status"] == "not_recorded"


def test_different_revenue_tag_abstains_and_latest_amendment_is_explicit(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    _save(path, _body(prior_tag="Revenues"), monkeypatch)
    assert financial_change.describe_sec(path, ticker="AAPL", issuer=CIK, as_of=NOW)["status"] == "no_comparable_facts"
    corrected = tmp_path / "corrected.db"
    _save(corrected, _body(amend=True), monkeypatch)
    item = financial_change.describe_sec(corrected, ticker="AAPL", issuer=CIK, as_of=NOW)
    assert item["metrics"]["revenue"]["current"] == "110"
    assert item["metrics"]["revenue"]["current_accession"] == "0000320193-26-000002"


def test_future_filing_and_wrong_issuer_cannot_populate_card(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    body = _body()
    body["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][1]["filed"] = "2026-12-01"
    _save(path, body, monkeypatch)
    assert financial_change.describe_sec(path, ticker="AAPL", issuer=CIK, as_of=NOW)["status"] == "no_comparable_facts"
    assert financial_change.describe_sec(path, ticker="MSFT", issuer="0000789019", as_of=NOW)["status"] == "not_recorded"


def test_us_watchlist_route_requires_observed_map_and_own_favorite(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from signal_desk import api
    from signal_desk.ingest import sec_issuer_map as secmap

    monkeypatch.setattr(api, "_uid", lambda _request: 7)
    monkeypatch.setattr(api, "_kst_now", lambda: dt.datetime.fromisoformat(NOW))
    monkeypatch.setattr(secmap, "_now", lambda: NOW)
    monkeypatch.setattr(api.db, "fav_list", lambda _uid: [{"kind": "ticker", "key": "AAPL"}])
    monkeypatch.setattr(secmap, "DEFAULT_ARCHIVE", tmp_path / "map.db")
    monkeypatch.setattr(financial_change, "DEFAULT_ARCHIVE", tmp_path / "facts.db")
    assert api.watchlist_financial_change_get(object(), "us", "AAPL")["status"] == "issuer_unmapped"
    mapping = {str(i): {"ticker": f"T{i:04d}", "cik_str": i + 100000, "title": f"Issuer {i}"}
               for i in range(1000)}
    mapping["1000"] = {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}
    secmap.archive(secmap.DEFAULT_ARCHIVE, json.dumps(mapping).encode(), observed_at=NOW)
    _save(financial_change.DEFAULT_ARCHIVE, _body(), monkeypatch)
    assert api.watchlist_financial_change_get(object(), "us", "AAPL")["status"] == "comparison"
    with pytest.raises(HTTPException) as exc:
        api.watchlist_financial_change_get(object(), "us", "MSFT")
    assert exc.value.status_code == 403
