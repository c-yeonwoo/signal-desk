from signal_desk import store
from signal_desk.ingest import dart, krx_open_api


def test_fetch_fundamentals_combines_per_pbr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]

    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(
        dart, "fundamentals",
        lambda ticker, corp_code, bsns_year: {"roe": 10.0, "net_income": 1000.0, "equity": 5000.0},
    )
    monkeypatch.setattr(krx_open_api, "market_caps", lambda: {"005930": 20000.0})

    out = store.fetch_fundamentals(universe)
    assert out["005930"]["per"] == round(20000.0 / 1000.0, 2)
    assert out["005930"]["pbr"] == round(20000.0 / 5000.0, 2)
    assert store.load_fundamentals() == out


def test_fetch_fundamentals_skips_per_when_net_income_negative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]

    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(
        dart, "fundamentals",
        lambda ticker, corp_code, bsns_year: {"net_income": -500.0, "equity": 5000.0},
    )
    monkeypatch.setattr(krx_open_api, "market_caps", lambda: {"005930": 20000.0})

    out = store.fetch_fundamentals(universe)
    assert "per" not in out["005930"]  # 적자 기업은 PER 계산 안 함(업계 관례)
    assert "pbr" in out["005930"]


def test_fetch_fundamentals_without_mktcap_still_returns_dart_metrics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]

    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(dart, "fundamentals", lambda ticker, corp_code, bsns_year: {"roe": 10.0})
    monkeypatch.setattr(krx_open_api, "market_caps", lambda: {})

    out = store.fetch_fundamentals(universe)
    assert out["005930"] == {"roe": 10.0}
