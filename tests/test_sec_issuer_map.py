"""SEC ticker associations are official observations, not guaranteed PIT identities."""

import datetime as dt
import json
import sqlite3

from signal_desk.ingest import sec_issuer_map as secmap


NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)


def official_map(*, ambiguous=False):
    rows = {str(i): {"ticker": f"T{i:04d}", "cik_str": 100000 + i,
                     "title": f"Test issuer {i}"} for i in range(1001)}
    rows["1001"] = {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}
    if ambiguous:
        rows["1002"] = {"ticker": "AAPL", "cik_str": 12345, "title": "Other issuer"}
    return json.dumps(rows).encode()


def test_official_map_observed_only_and_ambiguous_abstains(tmp_path, monkeypatch):
    path = tmp_path / "map.db"
    monkeypatch.setattr(secmap, "_now", lambda: NOW.isoformat())
    assert secmap.lookup(path, ticker="AAPL", as_of=NOW.isoformat())["status"] == "not_recorded"
    saved = secmap.archive(path, official_map(), observed_at=NOW.isoformat())
    assert saved["tickers"] == 1002
    item = secmap.lookup(path, ticker="AAPL", as_of=NOW.isoformat())
    assert item["status"] == "mapped" and item["cik"] == "0000320193"
    assert not item["strict_pit_eligible"] and not item["live_eligible"]
    assert secmap.lookup(path, ticker="AAPL", as_of="2026-10-02T23:59:59Z")["status"] == "not_recorded"
    assert secmap.lookup(path, ticker="AAPL", as_of="2026-11-10T00:00:00Z")["status"] == "stale"
    secmap.archive(path, official_map(ambiguous=True), observed_at=(NOW + dt.timedelta(days=1)).isoformat())
    assert secmap.lookup(path, ticker="AAPL", as_of=(NOW + dt.timedelta(days=1)).isoformat())["status"] == "ambiguous"


def test_corrupted_map_and_truncated_source_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "map.db"
    monkeypatch.setattr(secmap, "_now", lambda: NOW.isoformat())
    secmap.archive(path, official_map(), observed_at=NOW.isoformat())
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sec_issuer_map_observations SET raw_zlib=?", (b"broken",))
    assert secmap.lookup(path, ticker="AAPL", as_of=NOW.isoformat())["status"] == "archive_error"
    try:
        secmap.parse(b'{"0":{"ticker":"AAPL","cik_str":320193,"title":"Apple"}}')
    except ValueError:
        pass
    else:
        raise AssertionError("partial SEC map accepted")


def test_missing_contact_never_fetches(monkeypatch, tmp_path):
    monkeypatch.setattr(secmap.edgar, "available", lambda: False)
    monkeypatch.setattr(secmap.edgar, "_get", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    assert secmap.collect(tmp_path / "map.db") == {"status": "missing_contact", "requested": 0}
