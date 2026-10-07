from signal_desk import bot


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
