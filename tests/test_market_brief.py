"""One-card market reading must be dated, read-only, and fail closed on stale bars."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from signal_desk import api, market_brief


def _bars(last="2026-10-02", count=61):
    dates = [f"2026-07-{(i % 28) + 1:02d}" for i in range(count - 1)] + [last]
    prices = [100.0 + i for i in range(count)]
    return prices, dates


def test_fresh_market_card_uses_same_session_bars_and_dated_flow():
    prices, dates = _bars()
    out = market_brief.build(
        "kr", prices={"A": prices, "B": prices}, dates={"A": dates, "B": dates},
        tickers=["A", "B"], expected="2026-10-02", previous="2026-10-01",
        flow={"smart_net_20d": -2.3, "as_of": "2026-10-01"},
        selection={"buy_count": 1, "strong_buy_count": 0, "slots": 2,
                   "computed_at": "2026-10-02T07:00:00+00:00"},
        now=dt.datetime(2026, 10, 2, 9, tzinfo=dt.timezone.utc),
    )
    assert out["status"] == "ready"
    assert out["price_as_of"] == "2026-10-02"
    assert out["price_count"] == 2
    assert len(out["facts"]) == 3
    assert out["facts"][2]["as_of"] == "2026-10-01"
    assert out["selection"]["buy_count"] == 1
    assert out["not_order_advice"] is True


def test_stale_market_card_withholds_claims_and_buy_count():
    prices, dates = _bars(last="2026-10-01")
    out = market_brief.build("kr", prices={"A": prices}, dates={"A": dates},
                             tickers=["A"], expected="2026-10-02",
                             selection={"buy_count": 3})
    assert out["status"] == "stale"
    assert out["state"] is None and out["facts"] == [] and out["selection"] is None
    assert "2026-10-01" in out["unknown"][0]


def test_partial_market_card_excludes_old_symbol_and_reports_missing():
    prices, dates = _bars()
    old_prices, old_dates = _bars(last="2026-10-01")
    out = market_brief.build("us", prices={"A": prices, "B": old_prices},
                             dates={"A": dates, "B": old_dates}, tickers=["A", "B"],
                             expected="2026-10-02", previous="2026-10-01",
                             macro_indicators=[{"key": "NASDAQCOM", "change": 2.2,
                                                "asof": "2026-09-29"}])
    assert out["status"] == "partial"
    assert out["state"].startswith("평균가격 위 종목")
    assert out["price_count"] == 1
    assert out["selection"] is None
    assert len(out["facts"]) == 2  # old Nasdaq release is not today's reason
    assert any("1종목" in item for item in out["unknown"])
    assert any("나스닥" in item for item in out["unknown"])


def test_market_card_has_one_endpoint_and_defers_source_requests():
    html = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text(encoding="utf-8")
    assert "'/api/market-brief?market='" in html
    assert 'id="market-brief-sources"' in html
    start = html.split("async function startApp(){", 1)[1].split("// ===== 온보딩", 1)[0]
    assert "loadMacro();" not in start
    assert "loadIndustryPulse();" not in start


def test_market_brief_api_reuses_current_decisions_without_llm(monkeypatch):
    prices, dates = _bars()
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-02")
    monkeypatch.setattr(api.market_clock, "previous_session", lambda market, day: "2026-10-01")
    monkeypatch.setattr(api.store, "load_portfolio_close_bundle", lambda market: ({"A": prices}, {"A": dates}))
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(api, "_signals", lambda: [SimpleNamespace(kind="BUY", computed_at="2026-10-02T07:00:00+00:00")])
    monkeypatch.setattr(api, "_regime", lambda: {})
    monkeypatch.setattr(api, "_macro", lambda: {})
    monkeypatch.setattr(api.signalcfg, "effective_config", lambda *args, **kwargs: (object(), {}))
    monkeypatch.setattr(api, "selection_summary", lambda *args: {
        "mode": "rank", "rank_slots": 1, "cutoff_score": 1.4,
        "buy_threshold": 1.2, "rank_min_score": 0.0,
    })
    monkeypatch.setattr(api.llm, "complete", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM call")))
    out = api.market_brief_get("kr")
    assert out["status"] == "ready"
    assert out["selection"]["buy_count"] == 1
    assert out["selection_policy"]["cutoff_score"] == 1.4


def test_market_brief_api_does_not_compute_signals_when_prices_stale(monkeypatch):
    prices, dates = _bars(last="2026-10-01")
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-02")
    monkeypatch.setattr(api.market_clock, "previous_session", lambda market, day: "2026-10-01")
    monkeypatch.setattr(api.store, "load_portfolio_close_bundle", lambda market: ({"A": prices}, {"A": dates}))
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(api, "_signals", lambda: (_ for _ in ()).throw(AssertionError("stale signals")))
    out = api.market_brief_get("kr")
    assert out["status"] == "stale"
    assert out["selection"] is None
