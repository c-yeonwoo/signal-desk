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


def test_dividend_months_quarterly_excludes_annual():
    facts = {"facts": {"us-gaap": {"CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": [
        {"start": "2024-01-01", "end": "2024-03-31", "fp": "Q1", "form": "10-Q", "val": 0.77},
        {"start": "2024-04-01", "end": "2024-06-30", "fp": "Q2", "form": "10-Q", "val": 0.77},
        {"start": "2024-07-01", "end": "2024-09-30", "fp": "Q3", "form": "10-Q", "val": 0.77},
        {"start": "2024-10-01", "end": "2024-12-31", "fp": "Q4", "form": "10-K", "val": 0.77},
        {"start": "2024-01-01", "end": "2024-12-31", "fp": "FY", "form": "10-K", "val": 3.08}]}}}}}
    assert edgar._dividend_months(facts) == [3, 6, 9, 12]   # 연간(기간>100일) 항목 제외
    assert edgar._dividend_months({"facts": {"us-gaap": {}}}) == []  # 배당 없음
