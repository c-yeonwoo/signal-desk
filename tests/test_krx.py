from signal_desk.ingest import krx


def test_universe_uses_open_api_when_available(monkeypatch):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    calls = []

    def fake_universe_by_marketcap(bas_dd, limit=200):
        calls.append(bas_dd)
        return [{"ticker": "005930", "name": "삼성전자"}]

    monkeypatch.setattr(krx.krx_open_api, "universe_by_marketcap", fake_universe_by_marketcap)
    out = krx.universe()
    assert out == [{"ticker": "005930", "name": "삼성전자"}]
    assert len(calls) == 1  # 첫 시도에서 성공하면 추가 날짜 재시도 없음


def test_universe_falls_back_when_open_api_empty(monkeypatch):
    monkeypatch.setenv("KRX_API_KEY", "dummy")
    monkeypatch.setattr(krx.krx_open_api, "universe_by_marketcap", lambda bas_dd, limit=200: [])
    monkeypatch.setattr(krx, "_interim_universe", lambda: [{"ticker": "FALLBACK", "name": "폴백"}])
    out = krx.universe()
    assert out == [{"ticker": "FALLBACK", "name": "폴백"}]


def test_universe_skips_open_api_without_key(monkeypatch):
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(krx.krx_open_api, "universe_by_marketcap", lambda *a, **k: called.append(1))
    monkeypatch.setattr(krx, "_interim_universe", lambda: [{"ticker": "FALLBACK", "name": "폴백"}])
    out = krx.universe()
    assert out == [{"ticker": "FALLBACK", "name": "폴백"}]
    assert called == []
