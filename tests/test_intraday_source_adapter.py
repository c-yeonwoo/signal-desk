"""Offline source contracts; no signal, fill, or profitability claims."""

import base64
import datetime as dt
import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest

from signal_desk.broker import kis
from signal_desk.ingest import alpaca_research as source


START, END = "2024-06-03T13:30:00Z", "2024-06-03T13:35:00Z"


def _bar(**overrides):
    return {"t": START, "o": 192.0, "h": 193.0, "l": 191.0, "c": 192.5, "v": 100,
            **overrides}


def _alpaca(monkeypatch, rows=None, **body_fields):
    monkeypatch.setenv("APCA_API_KEY_ID", "private-alpaca-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "private-alpaca-secret")
    body = {"symbol": "AAPL", "bars": [_bar()] if rows is None else rows,
            "next_page_token": None, **body_fields}
    raw = json.dumps(body).encode()
    calls = []

    def read(request, timeout):
        calls.append(request)
        assert request.get_method() == "GET" and timeout == 15
        response = io.BytesIO(raw)
        response.status = 200
        return response

    monkeypatch.setattr(source, "_open", read)
    return raw, calls


def test_bar_window_preserves_raw_ohlcv_and_exclusive_end_without_session_claim(monkeypatch):
    raw, calls = _alpaca(monkeypatch, [_bar(t=f"2024-06-03T13:{minute}:00Z")
                                    for minute in range(30, 35)])
    result = source.bar_window("AAPL", START, END)
    assert len(calls) == 1 and calls[0].full_url.startswith("https://data.alpaca.markets/v2/stocks/AAPL/bars?")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0].full_url).query)
    assert params["timeframe"] == ["1Min"] and params["adjustment"] == ["raw"]
    assert params["feed"] == ["sip"] and params["limit"] == ["1000"]
    assert params["end"] == ["2024-06-03T13:34:59+00:00"]
    assert result["window"] == {"start": "2024-06-03T13:30:00+00:00",
                                "end": "2024-06-03T13:35:00+00:00", "end_exclusive": True}
    assert result["status"] == "observed" and result["bar_count"] == 5
    assert result["bars"][0] == {"t": START, "open": 192.0, "high": 193.0,
                                  "low": 191.0, "close": 192.5, "volume": 100}
    assert result["response_complete"] and not result["has_next_page"]
    assert result["coverage_status"] == "session_and_continuity_unverified"
    assert not result["live_order_eligible"] and not result["strict_pit_eligible"]
    assert base64.b64decode(result["raw_b64"]) == raw
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()


def test_bar_bounds_keep_fixed_panel_dates_and_one_ny_day():
    params = source.bar_parameters("AAPL", "2024-06-03T09:30:00-04:00", "2024-06-03T16:00:00-04:00")
    assert params["asof"] == "2024-06-03" and params["end"] == "2024-06-03T19:59:59+00:00"
    assert source.bar_parameters("AAPL", START, "2024-06-03T20:30:00Z")["feed"] == "sip"
    for symbol, start, end in (
        ("TSLA", START, END), ("AAPL/../orders", START, END),
        ("AAPL", START, START), ("AAPL", END, START),
        ("AAPL", "2024-06-03T13:30:00", END), ("AAPL", START, "2024-06-03T20:30:01Z"),
        ("AAPL", "2024-06-04T03:59:00Z", "2024-06-04T04:01:00Z"),
        ("AAPL", "2019-12-31T14:30:00Z", "2019-12-31T14:35:00Z"),
        ("AAPL", "2026-01-01T14:30:00Z", "2026-01-01T14:35:00Z"),
        ("AAPL", "2024-06-03T13:30:00.1Z", END),
    ):
        with pytest.raises(ValueError):
            source.bar_parameters(symbol, start, end)


def test_provider_pagination_and_sparse_rows_are_not_completed_session(monkeypatch):
    raw, calls = _alpaca(monkeypatch, [_bar()], next_page_token="more")
    result = source.bar_window("AAPL", START, END)
    assert len(calls) == 1 and result["has_next_page"] and not result["response_complete"]
    assert result["bar_count"] == 1 and len(result["bars"]) == 1
    assert result["coverage_status"] == "session_and_continuity_unverified"
    assert base64.b64decode(result["raw_b64"]) == raw
    _alpaca(monkeypatch, bars=None)
    empty = source.bar_window("AAPL", START, END)
    assert empty["status"] == "empty" and empty["bars"] == []


@pytest.mark.parametrize("rows", [
    [_bar(t=END)], [_bar(t="2024-06-03T13:29:00Z")], [_bar(t="2024-06-03T13:30:00.1Z")],
    [_bar(), _bar()], [_bar(t="2024-06-03T13:31:00Z"), _bar()],
    [_bar(o=None)], [_bar(h=191)], [_bar(l=193)], [_bar(c=float("nan"))],
    [_bar(o=True)], [_bar(v=-1)], [_bar(v=1.5)], [_bar(v=True)], [_bar(v="100")],
])
def test_invalid_or_inconsistent_bars_are_rejected_without_raw_reflection(monkeypatch, rows):
    _alpaca(monkeypatch, rows)
    result = source.bar_window("AAPL", START, END)
    assert result["status"] == "invalid_source_response" and "raw_b64" not in result


@pytest.mark.parametrize("body_fields", [
    {"symbol": "MSFT"}, {"echo": "private-alpaca-secret"}, {"bars": [_bar()] * 1001},
    {"next_page_token": False}, {"bars": None, "next_page_token": "more"},
])
def test_bar_schema_and_credentials_do_not_leak(monkeypatch, body_fields):
    _alpaca(monkeypatch, **body_fields)
    result = source.bar_window("AAPL", START, END)
    assert result["status"] == "invalid_source_response" and "raw_b64" not in result
    assert "private-alpaca" not in json.dumps(result)


def test_bar_missing_keys_rejection_cap_and_redirect_policy(monkeypatch):
    _, calls = _alpaca(monkeypatch)
    monkeypatch.delenv("APCA_API_KEY_ID")
    assert source.bar_window("AAPL", START, END)["status"] == "credentials_missing" and not calls
    _alpaca(monkeypatch)
    def reject(*args, **kwargs):
        raise urllib.error.HTTPError("https://data.alpaca.markets", 403, "secret", {},
                                     io.BytesIO(b"private-alpaca-secret"))
    monkeypatch.setattr(source, "_open", reject)
    result = source.bar_window("AAPL", START, END)
    assert result["status"] == "provider_rejected" and result["http_status"] == 403
    assert "raw_b64" not in result and "private-alpaca" not in json.dumps(result)
    _alpaca(monkeypatch)
    monkeypatch.setattr(source, "MAX_BYTES", 5)
    assert source.bar_window("AAPL", START, END)["status"] == "response_size_limit"
    assert source._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None


def _kis_row(**fields):
    return {"stck_cntg_hour": "090000", "stck_bsop_date": "20260714", "stck_prpr": "101",
            "stck_oprc": "100", "stck_hgpr": "102", "stck_lwpr": "99", "cntg_vol": "123", **fields}


def test_kis_ohlcv_and_requested_venue_are_kept_in_probe_and_day(monkeypatch):
    calls = []
    def respond(path, tr_id, creds, params):
        calls.append(params)
        return {"rt_cd": "0", "output2": [_kis_row()]}
    monkeypatch.setattr(kis, "_request", respond)
    result = kis.domestic_historical_minute_probe("005930", "2026-07-14", {"env": "real"})
    bar = result["ohlc_bars"][0]
    assert calls[0]["FID_COND_MRKT_DIV_CODE"] == result["venue"] == bar["venue"] == "J"
    assert [bar[key] for key in ("open", "high", "low", "close", "volume")] == [100, 102, 99, 101, 123]
    legacy_bars = [{"time": "09:00:00", "price": 101.0, "volume": 123, "date_reported": True}]
    assert result["bars"] == legacy_bars
    assert result["bars_sha256"] == hashlib.sha256(json.dumps(
        legacy_bars, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result["ohlc_bars_sha256"] == hashlib.sha256(json.dumps(
        result["ohlc_bars"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert bar["ohlc_status"] == "complete" and result["ohlc_complete_rows"] == 1
    day = kis.domestic_historical_minute_day("005930", "2026-07-14", {"env": "real"})
    assert day["status"] == "observed_day" and day["ohlc_complete_rows"] == 1 and day["venue"] == "J"
    assert day["bars"] == legacy_bars and day["bars_sha256"] == result["bars_sha256"]
    assert day["ohlc_bars"] == result["ohlc_bars"] and day["ohlc_bars_sha256"] == result["ohlc_bars_sha256"]


@pytest.mark.parametrize("overrides,status", [
    ({"stck_oprc": None, "stck_hgpr": "", "stck_lwpr": None}, "missing"),
    ({"stck_hgpr": "98"}, "invalid"), ({"stck_oprc": "NaN"}, "invalid"),
    ({"stck_lwpr": "0"}, "invalid"), ({"stck_oprc": True}, "invalid"),
])
def test_kis_missing_or_invalid_ohlc_is_not_replaced_with_close(monkeypatch, overrides, status):
    monkeypatch.setattr(kis, "_request", lambda *args: {"rt_cd": "0", "output2": [_kis_row(**overrides)]})
    result = kis.domestic_historical_minute_probe("005930", "2026-07-14", {"env": "real"})
    bar = result["ohlc_bars"][0]
    assert result["bars"][0]["price"] == bar["close"] == 101 and bar["volume"] == 123
    assert bar["ohlc_status"] == status and result[f"ohlc_{status}_rows"] == 1
    assert any(bar[key] is None for key in ("open", "high", "low"))
    assert result["invalid_rows"] == 0  # Close-only legacy samples remain explicitly close-only.


def test_kis_source_expansion_does_not_open_protected_dates_or_demo(monkeypatch):
    monkeypatch.setattr(kis, "_request", lambda *args: pytest.fail("must not request"))
    assert kis.domestic_historical_minute_probe("005930", "2026-08-04", {"env": "real"})["status"] == "outside_development_window"
    assert kis.domestic_historical_minute_probe("005930", "2026-07-14", {"env": "demo"})["status"] == "unavailable"


def test_kis_ohlc_enrichment_keeps_legacy_archive_identity_and_price_revision_conflict(tmp_path, monkeypatch):
    from signal_desk import db
    from signal_desk.signals import intraday_opportunity

    monkeypatch.setattr(db, "DB", tmp_path / "legacy-minute-days.db")
    minutes = [dt.datetime(2026, 7, 14, 9) + dt.timedelta(minutes=i) for i in range(391)]
    corrected_price = "101"
    corrected_high = "104"

    def respond(path, tr_id, creds, params):
        selected = [minute for minute in minutes if minute.strftime("%H%M%S") <= params["FID_INPUT_HOUR_1"]][-120:]
        return {"rt_cd": "0", "output2": [_kis_row(
            stck_cntg_hour=minute.strftime("%H%M%S"), stck_hgpr=corrected_high,
            stck_prpr=corrected_price if minute.strftime("%H%M%S") == "090100" else "101")
            for minute in reversed(selected)]}

    monkeypatch.setattr(kis, "_request", respond)
    first_day = kis.domestic_historical_minute_day("005930", "2026-07-14", {"env": "real"})
    # This is the exact old close-only representation. No OHLC was in v1 archives.
    legacy = {key: value for key, value in first_day.items() if not key.startswith("ohlc_") and key != "venue"}
    assert all(set(bar) == {"time", "price", "volume", "date_reported"} for bar in legacy["bars"])
    first = db.intraday_research_day_put(legacy)
    replay_before = intraday_opportunity.historical_minute_replay(legacy)
    assert first["status"] == "preserved" and replay_before["status"] == "counterfactual_only"

    corrected_high = "105"  # Added range information is not a close-price revision.
    fresh = kis.domestic_historical_minute_day("005930", "2026-07-14", {"env": "real"})
    assert fresh["ohlc_bars_sha256"] != first_day["ohlc_bars_sha256"]
    assert fresh["bars_sha256"] == legacy["bars_sha256"]
    preserved = db.intraday_research_day_put(fresh)
    assert preserved == first and preserved["revisions"] == 1
    restored = db.intraday_research_day_get("005930", "2026-07-14")
    assert restored == legacy and "ohlc_bars" not in restored  # No false OHLC archive claim.
    assert intraday_opportunity.historical_minute_replay(restored) == replay_before

    corrected_price = "103"  # A genuine close revision still blocks arbitrary source selection.
    revised = kis.domestic_historical_minute_day("005930", "2026-07-14", {"env": "real"})
    conflict = db.intraday_research_day_put(revised)
    assert conflict["status"] == "revision_conflict" and conflict["revisions"] == 2
    assert conflict["id"] != first["id"]
    assert db.intraday_research_day_get("005930", "2026-07-14") is None
