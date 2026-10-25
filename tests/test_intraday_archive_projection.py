"""An OHLC diagnostic is not an authenticated v1 close-bar archive extension."""
import hashlib
import json

from signal_desk import api, db


def test_first_archive_excludes_ohlc_sidecar_without_changing_response(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "source.db")
    bars = [{"time": "09:00:00", "price": 100, "volume": 100, "date_reported": True}]
    digest = hashlib.sha256(json.dumps(bars, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    legacy = {"ticker": "005930", "session": "2026-07-14", "status": "observed_day",
              "date_attested_by_rows": True, "bars": bars, "bars_sha256": digest}
    enriched = {**legacy, "venue": "J", "ohlc_bars": [{"time": "09:00:00", "open": 99}],
                "ohlc_bars_sha256": "diagnostic-only", "ohlc_complete_rows": 1,
                "ohlc_missing_rows": 0, "ohlc_invalid_rows": 0}
    first = api._archive_pilot_minute_day("005930", "2026-07-14", enriched)
    assert first["status"] == "preserved"
    assert db.intraday_research_day_get("005930", "2026-07-14") == legacy
    assert enriched["ohlc_bars"][0]["open"] == 99
    assert api._archive_pilot_minute_day("005930", "2026-07-14", legacy) == first
