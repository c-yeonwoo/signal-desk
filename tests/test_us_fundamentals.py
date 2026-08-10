"""US 시총·PER(Alpha Vantage) — 주식수 캐시 + 시총 재계산."""

from signal_desk import store
from signal_desk.ingest import alphavantage


def test_overview_none_without_key(monkeypatch):
    from signal_desk import config
    monkeypatch.setattr(config, "alphavantage_key", lambda: None)
    assert alphavantage.overview("AAPL") is None


def test_us_marketcaps_computes_from_shares_and_price(monkeypatch):
    monkeypatch.setattr(store, "load_us_fundamentals",
                        lambda: {"AAPL": {"shares": 1_000_000, "per": 30.0, "sector": "Tech"},
                                 "NVDA": {"shares": 2_000_000, "per": None, "sector": "Tech"},
                                 "NOPRICE": {"shares": 500, "per": 10.0, "sector": "X"}})
    prices = {"AAPL": [100.0, 150.0], "NVDA": [10.0, 20.0]}  # NOPRICE는 시세 없음
    mc = store.us_marketcaps(prices)
    # 퀄리티(F-Score)가 ROE를 파생하려면 순이익·자기자본이 함께 실려야 한다 — 예전엔 안 넘겨서
    # US 퀄리티 발동이 0/503이었다.
    assert mc["AAPL"] == {"mktcap": 150_000_000, "per": 30.0, "pbr": None,
                          "net_income": None, "equity": None}  # 100만주×150, per는 AV 폴백
    assert mc["NVDA"] == {"mktcap": 40_000_000, "per": None, "pbr": None,
                          "net_income": None, "equity": None}   # 200만주×20
    assert mc["NOPRICE"]["mktcap"] is None                       # 시세 없으면 시총 None


def test_us_marketcaps_empty_without_cache(monkeypatch):
    monkeypatch.setattr(store, "load_us_fundamentals", lambda: {})
    assert store.us_marketcaps({"AAPL": [100.0]}) == {}


# ---------- EDGAR companyfacts (US 재무) ----------
import json  # noqa: E402
from signal_desk.ingest import edgar  # noqa: E402


def _mock_edgar(monkeypatch):
    edgar._cik_map = None
    tickers_json = json.dumps({"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple"}}).encode()
    facts = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            {"fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-09-28", "val": 93_000_000_000},
            {"fy": 2023, "fp": "FY", "form": "10-K", "end": "2023-09-30", "val": 97_000_000_000}]}},
        "StockholdersEquity": {"units": {"USD": [
            {"fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-09-28", "val": 57_000_000_000}]}},
    }}}
    monkeypatch.setattr(edgar, "_get",
                        lambda url: tickers_json if "company_tickers" in url else json.dumps(facts).encode())


def test_edgar_fundamentals_latest_annual(monkeypatch):
    _mock_edgar(monkeypatch)
    f = edgar.fundamentals("AAPL")
    assert f["net_income"] == 93_000_000_000 and f["equity"] == 57_000_000_000  # 최신(2024) 연간
    assert edgar.fundamentals("UNKNOWN") is None                                 # CIK 없음


def test_edgar_backfill_and_per_pbr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    _mock_edgar(monkeypatch)
    store._write_json(store.US_FUNDAMENTALS_FILE, {"AAPL": {"shares": 15_000_000_000, "per": None, "sector": "Tech"}})
    assert store.fetch_us_fundamentals_edgar(["AAPL"], max_calls=10) == 1
    mc = store.us_marketcaps({"AAPL": [180.0, 200.0]})["AAPL"]  # 시총 = 150억주×$200 = $3T
    assert mc["mktcap"] == 3_000_000_000_000
    assert mc["per"] == 32.26 and mc["pbr"] == 52.63           # 3T/93B, 3T/57B
    assert store.fetch_us_fundamentals_edgar(["AAPL"], max_calls=10) == 0  # 이미 채워짐 → 스킵
