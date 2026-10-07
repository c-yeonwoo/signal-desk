"""실제 KR 봇·US API 계산 경로의 캡처를 격리 DB에서 재생한다.

외부 시세·뉴스·계좌는 고정한다. 운영 적재나 매매를 실행하는 테스트가 아니다.
"""

import datetime

from signal_desk import api, bot, db, store
from signal_desk.signals import decision_snapshot, engine


def _price_bundle(ticker):
    days = [(datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).isoformat()
            for i in range(260)]
    closes = [100.0 + i * 0.1 for i in range(260)]
    quote = 126.5
    return {ticker: [*closes, quote]}, {ticker: days}, {
        "captured_at": 1000.0, "quotes": {ticker: quote},
        "quote_updated": {ticker: 995.0},
        "quote_meta": {ticker: {"observation_id": f"{ticker}-quote-1", "provider": "test"}},
    }


def _quiet_gate_sources(monkeypatch):
    monkeypatch.setattr(store, "load_signal_history", lambda: [])
    monkeypatch.setattr(db, "kb_events_active", lambda: [])


def test_kr_bot_actual_capture_replays_without_price_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = _price_bundle("005930")
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(store, "kr_engine_inputs", lambda: {})
    _quiet_gate_sources(monkeypatch)

    read = {"eff_cfg": engine.SignalConfig(), "_price_bundle": bundle}
    universe, prices, results, _names, dates, quotes = bot._market_signals("kr", read)
    assert len(universe) == 1 and len(results) == 1
    assert (prices, dates, quotes) == bundle
    refs = decision_snapshot.persist_captured_decision(
        "kr", bundle, read["_decision_capture"])

    assert refs["structure_status"] == "verified"
    saved_quote = db.decision_artifact_get(refs["quote_delta_id"])["data"]["quotes"]["005930"]
    assert saved_quote["observation_id"] == "005930-quote-1"
    assert decision_snapshot.replay_signal_decision("kr", refs["signal_output_id"])["match"]
    assert {row["kind"] for row in db.decision_artifact_storage("kr")} == {
        "price_base", "quote_delta", "engine_input", "gate_input", "signal_output"}


def test_us_api_actual_capture_replays_without_price_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = _price_bundle("AAPL")
    monkeypatch.setattr(store, "load_engine_price_bundle", lambda market: bundle)
    monkeypatch.setattr(store, "load_us_universe", lambda: [{"ticker": "AAPL", "name": "Apple"}])
    monkeypatch.setattr(store, "us_marketcaps", lambda prices: {})
    monkeypatch.setattr(store, "attach_us_quality", lambda fundamentals: None)
    monkeypatch.setattr(store, "load_us_earnings_calendar", lambda: {})
    monkeypatch.setattr(api.kb, "sentiment_map", lambda: {})
    monkeypatch.setattr(api, "_sync_episode_state", lambda results, **kwargs: None)
    _quiet_gate_sources(monkeypatch)
    api._us_signals.cache_clear()
    try:
        snapshot = api._us_signals()
        assert list(snapshot) == ["AAPL"]
        refs = decision_snapshot.persist_captured_decision(
            "us", (snapshot.prices, snapshot.price_dates, snapshot.quote_snapshot),
            snapshot.decision_capture)
        assert refs["structure_status"] == "verified"
        replay = decision_snapshot.replay_signal_decision("us", refs["signal_output_id"])
        assert replay["match"] and replay["replayed_rows"] == 1
    finally:
        api._us_signals.cache_clear()
