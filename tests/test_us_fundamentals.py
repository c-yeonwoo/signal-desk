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
    assert mc["AAPL"] == {"mktcap": 150_000_000, "per": 30.0}   # 100만주 × 150
    assert mc["NVDA"] == {"mktcap": 40_000_000, "per": None}     # 200만주 × 20
    assert mc["NOPRICE"]["mktcap"] is None                       # 시세 없으면 시총 None


def test_us_marketcaps_empty_without_cache(monkeypatch):
    monkeypatch.setattr(store, "load_us_fundamentals", lambda: {})
    assert store.us_marketcaps({"AAPL": [100.0]}) == {}
