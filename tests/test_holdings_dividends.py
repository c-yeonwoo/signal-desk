"""내 보유종목 배당 집계(/api/holdings/dividends) — 보유×배당 조인, 통화별 ₩/$ 분리."""

from starlette.requests import Request

from signal_desk import api, db


def _req():
    return Request({"type": "http", "headers": [], "query_string": b""})


def test_holdings_dividends_splits_by_currency(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_uid", lambda r: 1)
    db.holdings_set(1, "005930", 100, 70000)   # KR 배당주
    db.holdings_set(1, "O", 50, 55)            # US 배당주(월배당)
    db.holdings_set(1, "NODIV", 10, 100)       # 배당 없음 → 제외

    monkeypatch.setattr(api.store, "kr_dividends",
                        lambda: {"005930": {"dps": 1444.0, "div_yield": 2.0, "div_months": [4]}})
    monkeypatch.setattr(api.store, "us_dividends",
                        lambda: {"O": {"dps": 3.0, "div_yield": 5.5, "div_months": [1,2,3,4,5,6,7,8,9,10,11,12]}})
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [{"ticker": "O", "name": "Realty Income"}])
    monkeypatch.setattr(api.us_ko, "name_ko", lambda t, n: n)

    out = api.holdings_dividends_get(_req())
    assert out["ready"] is True
    tickers = {i["ticker"] for i in out["items"]}
    assert tickers == {"005930", "O"}                      # 배당 없는 NODIV 제외
    assert set(out["totals"]) == {"KRW", "USD"}            # 통화 분리
    assert out["totals"]["KRW"]["annual"] == 144400.0      # 1444 × 100
    assert out["totals"]["USD"]["annual"] == 150.0         # 3 × 50
    assert out["totals"]["USD"]["monthly"] == 12.5         # 150 / 12


def test_holdings_dividends_empty_when_no_holdings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_uid", lambda r: 2)
    out = api.holdings_dividends_get(_req())
    assert out["ready"] is False and out["items"] == []
