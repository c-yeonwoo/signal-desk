"""Historical data access bounds, raw-source integrity and credential non-disclosure."""

import base64
import hashlib
import io
import json
import urllib.error

import pytest

from signal_desk.ingest import alpaca_research as source

START, END = "2023-02-03T14:31:30Z", "2023-02-03T14:31:35Z"


def test_window_is_fixed_panel_timezone_aware_and_outside_protected_data():
    params = source.parameters("AAPL", START, END)
    assert params["feed"] == "sip" and params["asof"] == "2023-02-03"
    assert source.parameters("FB", "2022-06-09T00:01:00Z", "2022-06-09T00:01:05Z")["asof"] == "2022-06-08"
    assert source.parameters("AAPL", START, "2023-02-03T14:36:30Z")["limit"] == "1000"
    assert source.parameters("AAPL", "2020-01-01T00:00:00Z", "2020-01-01T00:00:05Z")["feed"] == "sip"
    for symbol, start, end in (("AAPL/../../orders", START, END), ("AAPL", START, "2023-02-03T14:36:31Z"),
                              ("AAPL", "2026-10-17T14:31:30Z", "2026-10-17T14:31:35Z"),
                              ("AAPL", "2019-12-31T23:59:59Z", "2020-01-01T00:00:01Z"),
                              ("AAPL", "2023-02-03T14:31:30", END)):
        with pytest.raises(ValueError):
            source.parameters(symbol, start, end)


def _setup(monkeypatch, body=None):
    monkeypatch.setenv("APCA_API_KEY_ID", "private-alpaca-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "private-alpaca-secret")
    raw = json.dumps(body if body is not None else {"symbol": "AAPL", "quotes": [
        {"t": "2023-02-03T14:31:30.123456789Z", "bp": 100, "ap": 100.01, "bs": 1, "as": 1}],
        "next_page_token": None}).encode()
    calls = []
    def fake(request, timeout):
        calls.append(request)
        assert request.get_method() == "GET" and request.full_url.startswith("https://data.alpaca.markets/")
        response = io.BytesIO(raw)
        response.status = 200
        return response
    monkeypatch.setattr(source, "_open", fake)
    return raw, calls


def test_actual_sip_bytes_and_hash_are_preserved_without_authentication(monkeypatch):
    raw, calls = _setup(monkeypatch)
    result = source.quote_window("AAPL", START, END)
    assert len(calls) == 1 and result["status"] == "observed" and result["quote_count"] == 1
    assert base64.b64decode(result["raw_b64"]) == raw
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not result["live_order_eligible"] and result["observed_at"] >= result["requested_at"]
    assert "private-alpaca" not in json.dumps(result)


def test_explicit_null_empty_quotes_preserve_raw_without_inventing_a_fill(monkeypatch):
    raw, _ = _setup(monkeypatch, {"symbol": "AAPL", "quotes": None, "next_page_token": None})
    result = source.quote_window("AAPL", START, END)
    assert result["status"] == "empty" and result["quote_count"] == 0
    assert base64.b64decode(result["raw_b64"]) == raw


def test_missing_or_rejected_credentials_do_not_trigger_iex_fallback_or_leak_body(monkeypatch):
    _, calls = _setup(monkeypatch)
    monkeypatch.delenv("APCA_API_KEY_ID")
    assert source.quote_window("AAPL", START, END)["status"] == "credentials_missing" and calls == []
    _setup(monkeypatch)
    def reject(*args, **kwargs):
        raise urllib.error.HTTPError("https://data.alpaca.markets", 403, "private-alpaca-secret", {},
                                     io.BytesIO(b"private-alpaca-secret"))
    monkeypatch.setattr(source, "_open", reject)
    result = source.quote_window("AAPL", START, END)
    assert result["status"] == "provider_rejected" and result["http_status"] == 403
    assert "raw_b64" not in result and "private-alpaca" not in json.dumps(result)


@pytest.mark.parametrize("body", [{"symbol": "MSFT", "quotes": []},
    {"symbol": "AAPL", "quotes": [{"t": "2026-10-17T14:31:30Z"}]},
    {"symbol": "AAPL", "quotes": [{"t": "2023-02-03T14:31:35.000000001Z"}]},
    {"symbol": "AAPL", "quotes": [], "echo": "private-alpaca-secret"}])
def test_mismatched_future_or_secret_echo_is_not_returned(monkeypatch, body):
    _setup(monkeypatch, body)
    result = source.quote_window("AAPL", START, END)
    assert result["status"] == "invalid_source_response" and "raw_b64" not in result


def test_sip_nanoseconds_response_cap_and_redirects_are_not_silently_accepted(monkeypatch):
    assert source.clock_ns("2023-02-03T14:31:30.123456789Z") - source.clock_ns("2023-02-03T14:31:30Z") == 123456789
    assert source._NoRedirect().redirect_request(None, None, 302, "", {}, "https://different.example") is None
    _setup(monkeypatch)
    monkeypatch.setattr(source, "MAX_BYTES", 5)
    assert source.quote_window("AAPL", START, END)["status"] == "response_size_limit"


def test_admin_auth_and_bounds_precede_network_and_global_throttle(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api, "_admin_or_403", lambda request: (_ for _ in ()).throw(api.HTTPException(403)))
    with pytest.raises(api.HTTPException) as denied:
        api.alpaca_research_quotes_get(object(), "AAPL", START, END)
    assert denied.value.status_code == 403
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: pytest.fail("invalid window must not claim a lease"))
    with pytest.raises(api.HTTPException) as invalid:
        api.alpaca_research_quotes_get(object(), "AAPL", START, "2026-10-17T14:31:35Z")
    assert invalid.value.status_code == 422
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: False)
    with pytest.raises(api.HTTPException) as throttled:
        api.alpaca_research_quotes_get(object(), "AAPL", START, END)
    assert throttled.value.status_code == 429


def test_historical_news_keeps_revision_clock_and_exact_bytes(monkeypatch):
    body = {"news": [{"id": 123, "symbols": ["META"], "headline": "Meta Q1 EPS",
        "url": "https://example.com/original", "created_at": "2023-04-26T20:09:00Z",
        "updated_at": "2023-04-26T20:11:00Z"}], "next_page_token": "more"}
    raw, calls = _setup(monkeypatch, body)
    result = source.news_day("META", "2023-04-26")
    assert result["status"] == "observed" and result["news_count"] == 1 and result["has_next_page"]
    assert base64.b64decode(result["raw_b64"]) == raw
    assert calls[0].full_url.startswith("https://data.alpaca.markets/v1beta1/news?")
    assert "include_content=false" in calls[0].full_url and not result["strict_pit_eligible"]
    assert source.news_parameters("FB", "2020-01-29")["start"].startswith("2020-01-29")
    assert source.news_parameters("GOOG", "2020-10-29")["symbols"] == "GOOG"
    with pytest.raises(ValueError):
        source.parameters("GOOG", START, END)  # Class C must not replace GOOGL fills.
    for symbol, session, token in (("TSLA", "2023-01-01", ""), ("META", "2026-10-17", ""),
                                   ("FB", "2019-12-31", ""),
                                   ("META", "2023-04-26", "x&symbols=TSLA")):
        with pytest.raises(ValueError):
            source.news_parameters(symbol, session, token)


def test_news_auth_bounds_and_shared_throttle(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api, "_admin_or_403", lambda request: (_ for _ in ()).throw(api.HTTPException(403)))
    with pytest.raises(api.HTTPException) as denied:
        api.alpaca_research_news_get(object(), "META", "2023-04-26")
    assert denied.value.status_code == 403
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: pytest.fail("invalid must not lease"))
    with pytest.raises(api.HTTPException) as invalid:
        api.alpaca_research_news_get(object(), "META", "2026-10-17")
    assert invalid.value.status_code == 422
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: False)
    with pytest.raises(api.HTTPException) as throttled:
        api.alpaca_research_news_get(object(), "META", "2023-04-26")
    assert throttled.value.status_code == 429
