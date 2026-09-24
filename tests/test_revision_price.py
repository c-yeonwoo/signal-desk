"""As-observed consensus and the isolated revision/price research ranking."""

from datetime import datetime, timezone
import importlib

import exchange_calendars as xcals
import pandas as pd
import pytest

from signal_desk import store
from signal_desk.signals import revision_price


def test_consensus_as_of_ignores_later_revisions_and_unobserved_legacy(tmp_path, monkeypatch):
    archive = tmp_path / "observations.parquet"
    monkeypatch.setattr(store, "CONSENSUS_OBSERVATIONS_FILE", archive)
    pd.DataFrame([
        {"date": "2026-09-23", "ticker": "A", "fwd1_year": "202712", "fwd1_eps": 100,
         "observed_at": "2026-09-23T08:00:00+00:00", "content_hash": "a", "available_at_verified": False},
        {"date": "2026-09-23", "ticker": "A", "fwd1_year": "202712", "fwd1_eps": 120,
         "observed_at": "2026-09-23T10:00:00+00:00", "content_hash": "b", "available_at_verified": False},
        {"date": "2026-09-24", "ticker": "B", "fwd1_year": "202712", "fwd1_eps": 200,
         "observed_at": "2026-09-23T07:00:00+00:00", "content_hash": "c", "available_at_verified": False},
    ]).to_parquet(archive, index=False)
    old = store.load_consensus_as_of(datetime(2026, 9, 23, 9, tzinfo=timezone.utc))
    assert len(old) == 1 and old.iloc[0]["fwd1_eps"] == 100
    later = store.load_consensus_as_of(datetime(2026, 9, 23, 11, tzinfo=timezone.utc))
    assert len(later) == 1 and later.iloc[0]["fwd1_eps"] == 120
    with pytest.raises(ValueError):
        store.load_consensus_as_of(datetime(2026, 9, 23, 11))


def test_revision_price_shadow_uses_completed_session_and_sector_peers():
    cal = xcals.get_calendar("XKRX")
    sessions = [s.date().isoformat() for s in cal.sessions_window(pd.Timestamp("2026-09-23"), -6)]
    rows, dates_by, closes_by, sector_by = [], {}, {}, {}
    for i in range(1, 13):
        ticker = f"{i:06d}"
        rows += [
            {"date": sessions[-2], "ticker": ticker, "fwd1_year": "202712", "fwd1_eps": 100.0,
             "observed_at": "2026-09-22T09:00:00+00:00", "price_target_mean": None},
            {"date": sessions[-1], "ticker": ticker, "fwd1_year": "202712", "fwd1_eps": 100.0 + i,
             "observed_at": "2026-09-23T08:00:00+00:00", "price_target_mean": None},
        ]
        dates_by[ticker] = sessions
        closes_by[ticker] = [100.0] * 5 + [100.0 * (1 + (13 - i) / 100)]
        sector_by[ticker] = "same-sector"
    as_of = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    out = revision_price.build(observations=pd.DataFrame(rows), as_of=as_of,
                               dates_by=dates_by, closes_by=closes_by, sector_by=sector_by)
    assert out["research_ready"] and not out["live_eligible"]
    assert out["price_session"] == "2026-09-23" and out["eligible_count"] == 12
    assert out["candidates"][0]["ticker"] == "000012"
    assert out["candidates"][0]["eps_fiscal_year"] == "202712"

    dates_by["000012"] = dates_by["000012"] + [cal.next_session(pd.Timestamp(sessions[-1])).date().isoformat()]
    closes_by["000012"] = closes_by["000012"] + [10000.0]
    no_future_price = revision_price.build(observations=pd.DataFrame(rows), as_of=as_of,
                                           dates_by=dates_by, closes_by=closes_by,
                                           sector_by=sector_by)
    assert no_future_price["candidates"] == out["candidates"]

    # A future update cannot enter an earlier as-of replay even when its row date looks old.
    rows[-1]["observed_at"] = "2026-09-24T00:00:00+00:00"
    out_before_update = revision_price.build(observations=pd.DataFrame(rows), as_of=as_of,
                                             dates_by=dates_by, closes_by=closes_by,
                                             sector_by=sector_by)
    assert out_before_update["eligible_count"] == 11
    assert "000012" not in {r["ticker"] for r in out_before_update["candidates"]}


def test_revision_price_api_requires_admin_and_time_offset(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "research-admin@example.com")
    from signal_desk import db, api
    importlib.reload(db)
    importlib.reload(api)
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/revision-price").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/revision-price").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "research-admin@example.com", "pw": "abcdef12"})
    assert admin.get("/api/admin/research/revision-price?as_of=2026-09-23T10:00:00").status_code == 422
    response = admin.get("/api/admin/research/revision-price?as_of=2026-09-23T10:00:00%2B00:00")
    assert response.status_code == 200
    assert response.json()["research_ready"] is False
