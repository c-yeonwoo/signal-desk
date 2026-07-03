import pandas as pd

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
    assert out["005930"]["mktcap"] == 20000.0  # 시가총액도 저장(정렬·표기용)
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


def _write_prices(tmp_path, rows, cols):
    (tmp_path / "data" / "cache").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=cols).to_parquet(tmp_path / "data" / "cache" / "prices.parquet", index=False)


def test_load_quotes_computes_price_change_and_volume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path, [
        {"date": "2026-01-01", "ticker": "AAA", "open": 100, "close": 100, "volume": 1000},
        {"date": "2026-01-02", "ticker": "AAA", "open": 100, "close": 110, "volume": 3000},
    ], ["date", "ticker", "open", "close", "volume"])
    import json
    (tmp_path / "data" / "cache" / "fundamentals.json").write_text(
        json.dumps({"AAA": {"mktcap": 5.0e12}}), encoding="utf-8")
    q = store.load_quotes()["AAA"]
    assert q["price"] == 110 and q["prev_close"] == 100
    assert q["change_pct"] == 10.0
    assert q["vol"] == 3000 and q["vol_avg"] == 2000.0
    assert q["mktcap"] == 5.0e12


def test_fetch_fundamentals_history_by_year(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(dart, "fundamentals",
                        lambda ticker, corp_code, y: {"roe": 10.0, "net_income": 100.0, "_y": y})
    out = store.fetch_fundamentals_history(universe, years=["2024", "2025"])
    assert set(out["005930"]) == {"2024", "2025"}
    assert out["005930"]["2024"]["_y"] == "2024"
    assert store.load_fundamentals_history() == out


def test_load_quotes_graceful_without_volume_column(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path, [
        {"date": "2026-01-01", "ticker": "AAA", "open": 100, "close": 100},
    ], ["date", "ticker", "open", "close"])
    q = store.load_quotes()["AAA"]
    assert q["vol"] is None and q["vol_avg"] is None and q["mktcap"] is None
