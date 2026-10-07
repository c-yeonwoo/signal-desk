import time

from signal_desk import api


def test_live_quote_incident_sends_once_then_sends_recovery(monkeypatch):
    state = {}
    queued = []
    drained = []

    def kv_transform(key, transform):
        new, event = transform(state.get(key))
        if new is not None:
            state[key] = new
        return event

    monkeypatch.setattr(api.db, "kv_transform", kv_transform)
    monkeypatch.setattr(api.notify, "enqueue",
                        lambda text, **kwargs: queued.append((text, kwargs)) or True)
    monkeypatch.setattr(api.notify, "drain", lambda **kwargs: drained.append(kwargs))

    api._track_live_quote_incident(["kr"], {"kr": {"005930"}}, {}, reason="HTTPError")
    api._track_live_quote_incident(["kr"], {"kr": {"005930", "000660"}}, {}, reason="HTTPError")
    assert len(queued) == 1
    assert "국내 현재가 갱신 실패" in queued[0][0]
    assert "10분 넘은 현재가는 시그널에서 제외" in queued[0][0]

    api._track_live_quote_incident(["kr"], {"kr": set()}, {"kr": {"005930"}}, reason="응답 정상")
    api._track_live_quote_incident(["kr"], {"kr": set()}, {}, reason="응답 정상")
    assert len(queued) == 2
    assert "국내 현재가 갱신 복구" in queued[1][0]
    assert queued[0][1]["dedupe_key"] != queued[1][1]["dedupe_key"]
    assert len(drained) == 2


def test_refresh_alert_only_covers_stale_prices_that_were_previously_observed(monkeypatch):
    from signal_desk.ingest import toss

    tracked = []
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.db, "intraday_quotes_latest_ts",
                        lambda market, tickers: {"005930": int(time.time()) - 700} if tickers else {})
    monkeypatch.setattr(api, "_track_live_quote_incident",
                        lambda markets, missing, observed, **kwargs:
                        tracked.append((markets, missing, observed, kwargs)))
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "price_observations", lambda symbols: {})

    api._refresh_live_quotes(["kr"])
    assert tracked[0][1] == {"kr": {"005930"}, "us": set()}

    tracked.clear()
    monkeypatch.setattr(api.db, "intraday_quotes_latest_ts", lambda market, tickers: {})
    api._refresh_live_quotes(["kr"])
    assert tracked[0][1] == {"kr": set(), "us": set()}


def test_full_quote_coverage_excludes_invalid_and_unrequested_responses(monkeypatch):
    from signal_desk import store
    from signal_desk.ingest import toss

    monkeypatch.setattr(api.store, "load_universe", lambda: [])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [
        {"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "NVDA"}])
    monkeypatch.setattr(api.db, "intraday_quotes_latest_ts", lambda market, tickers: {})
    monkeypatch.setattr(api.db, "intraday_quotes_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.db, "kv_get", lambda key: api._kst_today())
    monkeypatch.setattr(api, "_track_live_quote_incident", lambda *args, **kwargs: None)
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "price_observations", lambda symbols: {
        "AAPL": {"price": 200.0}, "MSFT": {"price": float("nan")},
        "UNKNOWN": {"price": 1.0}})

    try:
        api._refresh_live_quotes(["us"])
        status = store.live_status()
        assert store.live_quotes_snapshot()["quotes"] == {"AAPL": 200.0}
        assert status["attempt_result"] == "ok"
        assert status["coverage"]["us"] == {
            "requested_count": 3, "received_count": 1, "missing_count": 2,
            "missing_sample": ["MSFT", "NVDA"]}
    finally:
        store.clear_live_quotes()
        store.note_live_attempt("closed")


def test_class_share_quote_is_requested_as_provider_symbol_but_stored_as_internal_id(
        tmp_path, monkeypatch):
    from signal_desk import store
    from signal_desk.ingest import toss

    symbols_file = tmp_path / "us_symbols.json"
    symbols_file.write_text('{"toss":{"BRK-B":"BRK.B","BF-B":"BF.B"}}')
    monkeypatch.setattr(store, "US_SYMBOLS_FILE", symbols_file)
    monkeypatch.setattr(store, "load_universe", lambda: [])
    monkeypatch.setattr(store, "load_us_universe", lambda: [
        {"ticker": "BRK-B"}, {"ticker": "BF-B"}, {"ticker": "PSKY"}])
    monkeypatch.setattr(api.db, "intraday_quotes_latest_ts", lambda market, tickers: {})
    recorded = []
    monkeypatch.setattr(api.db, "intraday_quotes_record",
                        lambda market, quotes, **kwargs: recorded.append((market, quotes)))
    monkeypatch.setattr(api.db, "kv_get", lambda key: api._kst_today())
    monkeypatch.setattr(api, "_track_live_quote_incident", lambda *args, **kwargs: None)
    monkeypatch.setattr(toss, "available", lambda: True)
    requested = []

    def observations(symbols):
        requested.extend(symbols)
        return {"BRK.B": {"price": 500.0}, "BF.B": {"price": 40.0},
                "BRK-B": {"price": 1.0}, "UNKNOWN": {"price": 1.0}}

    monkeypatch.setattr(toss, "price_observations", observations)
    try:
        api._refresh_live_quotes(["us"])
        assert requested == ["BF.B", "BRK.B", "PSKY"]
        assert store.live_quotes_snapshot()["quotes"] == {"BRK-B": 500.0, "BF-B": 40.0}
        assert set(recorded[0][1]) == {"BRK-B", "BF-B"}
        assert store.live_status()["coverage"]["us"] == {
            "requested_count": 3, "received_count": 2, "missing_count": 1,
            "missing_sample": ["PSKY"]}
    finally:
        store.clear_live_quotes()
        store.note_live_attempt("closed")


def test_held_class_share_quote_uses_same_mapping_without_changing_full_coverage(
        tmp_path, monkeypatch):
    from signal_desk import store
    from signal_desk.ingest import toss

    symbols_file = tmp_path / "us_symbols.json"
    symbols_file.write_text('{"toss":{"BRK-B":"BRK.B"}}')
    monkeypatch.setattr(store, "US_SYMBOLS_FILE", symbols_file)
    monkeypatch.setattr(api.db, "bot_position_tickers_market",
                        lambda market: {"BRK-B"} if market == "us" else set())
    monkeypatch.setattr(api.db, "holdings_tickers_market", lambda market: set())
    recorded = []
    monkeypatch.setattr(api.db, "intraday_quotes_record",
                        lambda market, quotes, **kwargs: recorded.append((market, quotes)))
    monkeypatch.setattr(toss, "available", lambda: True)
    requested = []
    monkeypatch.setattr(toss, "price_observations",
                        lambda symbols: (requested.extend(symbols) or {
                            "BRK.B": {"price": 510.0}, "UNREQUESTED": {"price": 1.0}}))
    store.note_live_attempt("ok", ["us"], requested_by_market={"us": {"BRK-B", "PSKY"}},
                            received_by_market={"us": {"PSKY"}})
    try:
        api._refresh_held_live_quotes(["us"])
        assert requested == ["BRK.B"]
        assert store.live_quotes_snapshot()["quotes"] == {"BRK-B": 510.0}
        assert set(recorded[0][1]) == {"BRK-B"}
        assert store.live_status()["coverage"]["us"]["received_count"] == 1
    finally:
        store.clear_live_quotes()
        store.note_live_attempt("closed")
