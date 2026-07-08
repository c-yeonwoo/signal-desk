"""배당 플래너(US) — EDGAR 주당배당 추출 + 배당수익률 계산."""

import json

from signal_desk import store
from signal_desk.ingest import edgar


def test_edgar_extracts_dps(monkeypatch):
    edgar._cik_map = None
    tickers = json.dumps({"0": {"ticker": "O", "cik_str": 726728, "title": "Realty Income"}}).encode()
    facts = {"facts": {"us-gaap": {
        "CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": [
            {"fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-12-31", "val": 3.08}]}},
        "NetIncomeLoss": {"units": {"USD": [
            {"fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-12-31", "val": 1000}]}},
    }}}
    monkeypatch.setattr(edgar, "_get",
                        lambda url: tickers if "company_tickers" in url else json.dumps(facts).encode())
    f = edgar.fundamentals("O")
    assert f["dps"] == 3.08 and f["net_income"] == 1000


def test_us_dividends_only_payers_and_yield(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    store._write_json(store.US_FUNDAMENTALS_FILE,
                      {"O": {"dps": 3.0}, "NODIV": {"dps": None}, "ZERO": {"dps": 0}})
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"O": [50.0, 60.0]})
    d = store.us_dividends()
    assert set(d) == {"O"}                           # 배당 있는 종목만
    assert d["O"]["dps"] == 3.0 and d["O"]["div_yield"] == 5.0  # 3 / 60
