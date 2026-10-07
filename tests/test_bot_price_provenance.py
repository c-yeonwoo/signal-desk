import time

from signal_desk import bot, store


def test_execution_price_evidence_links_matching_fresh_intraday_quote(monkeypatch):
    monkeypatch.setattr(bot, "_today", lambda market: "2026-10-07")
    evidence = bot._execution_price_evidence(
        "kr", "005930", 71000,
        dates_by_ticker={"005930": ["2026-10-06"]},
        quote_snapshot={
            "quotes": {"005930": 71000},
            "quote_updated": {"005930": 1000},
            "quote_meta": {"005930": {
                "observation_id": "quote-1", "provider": "toss",
                "source_timestamp": "2026-10-07T09:01:00+09:00",
                "source_time_verified": False,
            }},
        },
        now=1010,
    )

    assert evidence == {
        "price_basis": "intraday_provisional",
        "price_session": "2026-10-07",
        "price_observation_id": "quote-1",
        "price_provider": "toss",
        "price_received_at": 1000.0,
        "price_source_timestamp": "2026-10-07T09:01:00+09:00",
        "price_source_time_verified": False,
    }


def test_execution_price_evidence_falls_back_to_source_daily_session():
    evidence = bot._execution_price_evidence(
        "us", "AAPL", 200,
        dates_by_ticker={"AAPL": ["2026-10-05", "2026-10-06"]},
        quote_snapshot={"quotes": {"AAPL": 201}, "quote_updated": {"AAPL": 1000}},
        now=1010,
    )

    assert evidence["price_basis"] == "daily_close"
    assert evidence["price_session"] == "2026-10-06"
    assert evidence["price_observation_id"] is None


def test_bot_uses_price_array_value_without_reloading_other_market(monkeypatch):
    def unexpected_reload(_ticker):
        raise AssertionError("price array is already the selected market input")

    monkeypatch.setattr(bot.paper, "current_price", unexpected_reload)

    assert bot._live_price("005930", 71000) == 71000


def test_price_and_observation_survive_quote_change_after_capture(monkeypatch):
    received = time.time() - 5
    monkeypatch.setattr(store, "_kr_prices_raw", lambda: (
        {"005930": [70000.0]}, {"005930": ["2026-10-06"]}))
    first = {"quotes": {"005930": 71000.0}, "quote_updated": {"005930": received},
             "quote_meta": {"005930": {"observation_id": "first"}}}
    second = {"quotes": {"005930": 72000.0}, "quote_updated": {"005930": received + 1},
              "quote_meta": {"005930": {"observation_id": "second"}}}
    monkeypatch.setattr(store, "live_quotes_snapshot", lambda: first)

    prices, dates, captured = store.load_engine_price_bundle("kr")
    monkeypatch.setattr(store, "live_quotes_snapshot", lambda: second)
    evidence = bot._execution_price_evidence("kr", "005930", prices["005930"][-1],
                                              dates_by_ticker=dates, quote_snapshot=captured)

    assert prices["005930"] == [70000.0, 71000.0]
    assert evidence["price_observation_id"] == "first"
    assert evidence["price_basis"] == "intraday_provisional"


def test_stale_quote_is_not_appended_or_labeled_as_current(monkeypatch):
    monkeypatch.setattr(store, "_kr_prices_raw", lambda: (
        {"005930": [70000.0]}, {"005930": ["2026-10-06"]}))
    monkeypatch.setattr(store, "live_quotes_snapshot", lambda: {
        "quotes": {"005930": 71000.0}, "quote_updated": {"005930": time.time() - 601},
        "quote_meta": {"005930": {"observation_id": "stale"}}})

    prices, dates, captured = store.load_engine_price_bundle("kr")
    evidence = bot._execution_price_evidence("kr", "005930", prices["005930"][-1],
                                              dates_by_ticker=dates, quote_snapshot=captured)

    assert prices["005930"] == [70000.0]
    assert evidence["price_basis"] == "daily_close"
    assert evidence["price_session"] == "2026-10-06"
    assert evidence["price_observation_id"] is None


def test_us_price_bundle_keeps_its_own_close_date_and_quote(monkeypatch):
    monkeypatch.setattr(store, "_us_prices_raw", lambda: (
        {"AAPL": [200.0]}, {}, {"AAPL": ["2026-10-06"]}))
    monkeypatch.setattr(store, "live_quotes_snapshot", lambda: {
        "quotes": {"AAPL": 201.0}, "quote_updated": {"AAPL": time.time() - 5},
        "quote_meta": {"AAPL": {"observation_id": "us-first", "provider": "toss"}}})

    prices, dates, captured = store.load_engine_price_bundle("us")
    evidence = bot._execution_price_evidence("us", "AAPL", prices["AAPL"][-1],
                                              dates_by_ticker=dates, quote_snapshot=captured)

    assert prices["AAPL"] == [200.0, 201.0]
    assert dates["AAPL"] == ["2026-10-06"]
    assert evidence["price_basis"] == "intraday_provisional"
    assert evidence["price_observation_id"] == "us-first"


def test_kr_regime_engine_and_gate_share_one_price_capture(monkeypatch):
    price_bundle = ({"005930": [70000.0, 71000.0]}, {"005930": ["2026-10-06"]},
                    {"quotes": {"005930": 71000.0}, "quote_updated": {},
                     "quote_meta": {"005930": {"observation_id": "first"}}})
    calls = []

    def capture(market):
        calls.append(market)
        if len(calls) != 1:
            raise AssertionError("KR prices were loaded twice in one decision")
        return price_bundle

    monkeypatch.setattr(store, "load_engine_price_bundle", capture)
    monkeypatch.setattr(bot, "_market_read", lambda prices: (
        {"eff_cfg": None, "context": {"regime_price": prices["005930"][-1]}}))
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(store, "kr_engine_inputs", lambda: {})
    seen = {}
    monkeypatch.setattr(bot.engine, "evaluate", lambda _universe, prices, _fundamentals, **_kwargs: (
        seen.update(engine_prices=prices) or []))
    monkeypatch.setattr(bot.execution_gate, "apply_from_store", lambda _signals, **kwargs: (
        seen.update(gate_prices=kwargs["price_bundle"][0]) or []))

    read = bot._market_read_for("kr")
    _, prices, _, _, dates, observed = bot._market_signals("kr", read)

    assert calls == ["kr"]
    assert read["context"]["regime_price"] == 71000.0
    assert seen["engine_prices"] is seen["gate_prices"] is prices
    assert dates is price_bundle[1] and observed is price_bundle[2]
    assert read["_decision_capture"]["engine_inputs"]["universe"][0]["ticker"] == "005930"
    assert read["_decision_capture"]["engine_inputs"]["today"] is not None
