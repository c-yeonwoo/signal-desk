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
