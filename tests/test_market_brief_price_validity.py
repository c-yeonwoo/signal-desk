"""The headline must not revive a BUY that the stock list would defer."""

import datetime as dt
from types import SimpleNamespace

import pytest

from signal_desk import api
from signal_desk.signals.engine import SignalResult


@pytest.fixture
def brief(monkeypatch):
    def setup(market, *, closed=False, missing=False, stale=False, partial=False, stale_close=False):
        now = dt.datetime.fromisoformat(
            "2026-10-28T12:00:00+00:00" if closed else
            "2026-10-28T02:00:00+00:00" if market == "kr" else
            "2026-10-28T15:00:00+00:00")
        close = "2026-10-28" if closed else "2026-10-27"

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(api, "datetime", SimpleNamespace(datetime=Clock, timezone=dt.timezone))
        ps, ds = [100.] * 61, ["2026-07-01"] * 60 + [close]
        monkeypatch.setattr(api.store, "load_portfolio_close_bundle",
                            lambda market: ({"AAA": ps, "BBB": ps}, {"AAA": ds, "BBB": ds}))
        universe = [{"ticker": t, "name": t} for t in ("AAA", "BBB")]
        monkeypatch.setattr(api.store, "load_universe", lambda: universe)
        monkeypatch.setattr(api.store, "load_us_universe", lambda: universe)
        monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
        monkeypatch.setattr(api.store, "load_macro", lambda: [])
        monkeypatch.setattr(api, "_regime", lambda: {})
        monkeypatch.setattr(api, "_macro", lambda: {})
        cfg = api.signalcfg.get_config()
        monkeypatch.setattr(api.signalcfg, "effective_config", lambda *a, **kw: (cfg, {}))
        monkeypatch.setattr(api.llm, "complete", lambda *a, **kw: pytest.fail("LLM called"))
        results = []
        for ticker in ("AAA", "BBB"):
            source = now - dt.timedelta(seconds=601 if stale and (not partial or ticker == "AAA") else 10)
            proof = {"price_basis": "intraday_provisional", "price_session": "2026-10-28",
                     "price_observation_id": ticker, "price_received_at": now.timestamp() - 5,
                     "price_source_timestamp": None if missing else source.isoformat(),
                     "price_source_time_verified": False}
            signal = SignalResult(
                ticker=ticker, name=ticker, kind="BUY", score=2., confidence=.8,
                technical_score=1., fundamental_score=0., has_fundamental=False,
                rank_eligible=True)
            signal.computed_at = now.isoformat()
            signal.signal_close_date = "2026-10-26" if stale_close else close
            signal.signal_price_basis = "stored_close" if closed else "intraday_quote"
            signal.signal_input_price = 100.
            signal.signal_quote_received_at = now.timestamp() - 5
            signal.signal_price_evidence = proof
            results.append(signal)
        calls = []
        def snapshot():
            calls.append(True)
            return results if market == "kr" else {s.ticker: s for s in results}
        monkeypatch.setattr(api, "_signals" if market == "kr" else "_us_signals", snapshot)
        return results, calls
    return setup


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("fault", ["missing", "stale"])
def test_headline_withholds_all_invalid_quotes(brief, market, fault):
    results, calls = brief(market, **{fault: True})
    card = api.market_brief_get(market)
    assert card["status"] == "ready"  # Daily market description remains available.
    assert card["selection"] is None
    assert card["selection_deferred_count"] == 2
    assert any("가격" in message for message in card["unknown"])
    assert len(calls) == 1
    assert all(s.kind == "BUY" and s.rank_eligible for s in results)


@pytest.mark.parametrize("market", ["kr", "us"])
def test_headline_counts_only_valid_decisions_and_discloses_missing(brief, market):
    results, calls = brief(market, stale=True, partial=True)
    card = api.market_brief_get(market)
    assert card["selection"]["buy_count"] == 1
    assert card["selection"]["price_basis"] == "intraday_quote"
    assert card["selection"]["reference_only"] is False
    assert card["selection_deferred_count"] == 1
    assert len(calls) == 1
    assert all(s.kind == "BUY" for s in results)


def test_closed_market_preserves_explicit_close_reference(brief):
    brief("kr", closed=True)
    card = api.market_brief_get("kr")
    assert card["selection"]["buy_count"] == 2
    assert card["selection"]["price_basis"] == "stored_close"
    assert card["selection"]["reference_only"] is True
    assert card["selection_deferred_count"] == 0


def test_closed_market_does_not_revive_an_old_signal_when_market_bars_are_fresh(brief):
    brief("kr", closed=True, stale_close=True)
    card = api.market_brief_get("kr")
    assert card["status"] == "ready"
    assert card["selection"] is None
    assert card["selection_deferred_count"] == 2
