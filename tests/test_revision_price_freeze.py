"""R13a must preserve first-seen FY/price inputs and reject reconstructed history."""

import datetime as dt
import hashlib

import pandas as pd
from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import revision_price_freeze as frozen


SESSION = "2026-09-28"
NOW = dt.datetime(2026, 9, 28, 9, 30, tzinfo=dt.timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _inputs():
    rows, candidates, prices, dates = [], [], {}, {}
    for i in range(1, 13):
        ticker = f"T{i:02d}"
        previous = {"date": "2026-09-25", "ticker": ticker, "fwd1_year": "202712",
                    "fwd1_eps": 100.0, "observed_at": "2026-09-25T08:00:00+00:00",
                    "content_hash": _hash(f"old{i}")}
        current = {"date": SESSION, "ticker": ticker, "fwd1_year": "202712",
                   "fwd1_eps": 100.0 + i, "observed_at": "2026-09-28T08:00:00+00:00",
                   "content_hash": _hash(f"new{i}"), "source_published_at": None,
                   "available_at_verified": False}
        rows.extend([previous, current])
        candidates.append({"ticker": ticker, "revision_date": SESSION,
                           "eps_fiscal_year": "202712", "eps_revision_pct": float(i),
                           "sector": "test", "sector_relative_return_pct": 0.0,
                           "research_gap": round((13-i)/12, 4)})
        prices[ticker] = [100.0]
        dates[ticker] = [SESSION]
    result = {"research_ready": True, "price_session": SESSION,
              "version": "revision-price-shadow-v1", "revision_version": "same-fiscal-year-v2",
              "candidates": candidates}
    return result, pd.DataFrame(rows), prices, dates


def test_proof_freezes_same_fy_observed_versions_and_raw_prices():
    result, obs, prices, dates = _inputs()
    payload = frozen.freeze_inputs(result, obs, prices, dates, NOW)
    assert payload["ready"] and payload["proven_candidates"] == 12
    assert payload["policies"]["revision_unreacted_price"] == ["T01"]
    assert payload["policies"]["eps_revision_only"] == ["T12"]
    assert payload["selected"]["T01"]["revision_content_hash"] == _hash("new1")
    assert payload["selected"]["T12"]["prior_eps"] == 100
    assert payload["source_available_at_verified"] is False
    assert payload["notional"] == frozen.NOTIONAL
    assert payload["cost_assumptions"]["market"] == "kr"


def test_unproven_or_backfilled_prior_version_cannot_enter_cohort():
    result, obs, prices, dates = _inputs()
    obs.loc[(obs.ticker == "T01") & (obs.date == SESSION), "content_hash"] = None
    bad = frozen.freeze_inputs(result, obs, prices, dates, NOW)
    assert bad["ready"] and bad["proven_candidates"] == 11 and "T01" in bad["excluded_tickers"]
    obs.loc[(obs.ticker == "T02") & (obs.date == "2026-09-25"), "observed_at"] = "2026-09-28T09:00:00+00:00"
    bad = frozen.freeze_inputs(result, obs, prices, dates, NOW)
    assert bad["ready"] and bad["proven_candidates"] == 10 and "T02" in bad["excluded_tickers"]
    obs.loc[(obs.ticker == "T03") & (obs.date == SESSION), "fwd1_year"] = "202812"
    assert frozen.freeze_inputs(result, obs, prices, dates, NOW)["ready"] is False


def test_capture_once_and_admin_route_never_reconstructs_current_prices(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    result, obs, prices, dates = _inputs()
    monkeypatch.setattr(frozen.store, "load_consensus_as_of", lambda now: obs)
    monkeypatch.setattr(frozen.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    monkeypatch.setattr(frozen.revision_price, "build", lambda **kwargs: result)
    monkeypatch.setattr(api, "_rl_hits", {})
    monkeypatch.setenv("ADMIN_EMAILS", "revision-freeze@example.com")
    assert frozen.capture(NOW)["saved"] == 1
    first = db.revision_price_get(SESSION)
    prices["T01"][0] = 9999.0
    assert frozen.capture(NOW)["saved"] == 0
    assert db.revision_price_get(SESSION) == first
    assert frozen.capture(dt.datetime(2026, 9, 30, 9, 30, tzinfo=dt.timezone.utc))["saved"] == 0

    monkeypatch.setattr(api.store, "load_price_series", lambda: (_ for _ in ()).throw(AssertionError("replay")))
    assert TestClient(api.app).get("/api/admin/research/revision-price").status_code == 401
    admin = TestClient(api.app)
    assert admin.post("/api/auth/signup", json={"email": "revision-freeze@example.com", "pw": "abcdef12"}).status_code == 200
    response = admin.get("/api/admin/research/revision-price")
    assert response.status_code == 200
    assert response.json()["research_ready"] and response.json()["candidates"][0]["ticker"] == "T01"
    past = admin.get("/api/admin/research/revision-price?as_of=2026-09-25T09:00:00%2B00:00")
    assert past.status_code == 200 and past.json()["research_ready"] is False
