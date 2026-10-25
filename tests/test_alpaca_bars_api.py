"""Admin-only, bounded source probe: never a live strategy or unbounded backfill."""
import pytest

from signal_desk import api
from signal_desk.ingest import alpaca_research as source

START = "2024-06-03T13:30:00Z"
END = "2024-06-03T13:35:00Z"


def test_auth_precedes_metadata_or_provider(monkeypatch):
    monkeypatch.setattr(api, "_admin_or_403", lambda request: (_ for _ in ()).throw(api.HTTPException(403)))
    monkeypatch.setattr(source, "availability", lambda: pytest.fail("auth first"))
    for args in ((), ("AAPL", START, END)):
        with pytest.raises(api.HTTPException) as exc:
            api.alpaca_research_bars_get(object(), *args)
        assert exc.value.status_code == 403


def test_metadata_does_not_query_or_claim_source_lease(monkeypatch):
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: pytest.fail("metadata must not lease"))
    monkeypatch.setattr(source, "bar_window", lambda *a: pytest.fail("metadata must not fetch"))
    result = api.alpaca_research_bars_get(object())
    assert result["timeframe"] == "1Min" and result["adjustment"] == "raw"
    assert result["max_window_seconds"] == 25200
    assert result["live_order_eligible"] is False
    assert result["coverage_status"] == "session_and_continuity_unverified"


@pytest.mark.parametrize("args", [("TSLA", START, END), ("AAPL", START, ""),
    ("AAPL", START, "2026-10-25T13:35:00Z"), ("AAPL", START, "2024-06-03T20:31:00Z")])
def test_invalid_bounds_do_not_claim_lease_or_fetch(monkeypatch, args):
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: pytest.fail("bounds first"))
    monkeypatch.setattr(source, "bar_window", lambda *a: pytest.fail("bounds first"))
    with pytest.raises(api.HTTPException) as exc:
        api.alpaca_research_bars_get(object(), *args)
    assert exc.value.status_code == 422


def test_shared_source_throttle_and_exact_window_forwarding(monkeypatch):
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: False)
    monkeypatch.setattr(source, "bar_window", lambda *a: pytest.fail("throttled must not fetch"))
    with pytest.raises(api.HTTPException) as exc:
        api.alpaca_research_bars_get(object(), "AAPL", START, END)
    assert exc.value.status_code == 429
    leases, calls = [], []
    monkeypatch.setattr(api.db, "lease_claim", lambda *a, **k: leases.append((a, k)) or True)
    monkeypatch.setattr(source, "bar_window", lambda *a: calls.append(a) or {"status": "credentials_missing"})
    assert api.alpaca_research_bars_get(object(), "AAPL", START, END) == {"status": "credentials_missing"}
    assert calls == [("AAPL", START, END)]
    assert leases[0][0][0] == "alpaca_historical_quotes_global" and leases[0][1]["lease_sec"] == 1
