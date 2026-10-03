import datetime

import pandas as pd

from signal_desk import store
from signal_desk.ingest import dart, krx, krx_open_api


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
    assert out["005930"]["roe"] == 10.0
    assert out["005930"]["fiscal_year"] == store.latest_annual_report_year()


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


def test_fetch_prices_incremental_upsert(tmp_path, monkeypatch):
    """full 백필 → 증분 재수집 시 마지막 저장일부터만 다시 받아 append하고, (ticker,date) 중복은
    keep='last'로 덮는다(잠정 종가→확정치)."""
    monkeypatch.chdir(tmp_path)
    uni = [{"ticker": "005930", "name": "삼성"}]
    catalog = {"2026-07-01": 10.0, "2026-07-02": 11.0, "2026-07-03": 12.0}
    calls = []

    def fake_ohlcv(ticker, start, end):
        calls.append(start)
        return [{"date": d, "open": 1.0, "close": c, "volume": 100.0}
                for d, c in sorted(catalog.items()) if d.replace("-", "") >= start]

    monkeypatch.setattr(krx, "ohlcv", fake_ohlcv)

    df1 = store.fetch_prices(uni, full=True)
    assert list(df1.sort_values("date")["close"]) == [10.0, 11.0, 12.0]

    catalog["2026-07-03"] = 12.5   # 마지막 저장일 종가 확정치로 갱신
    catalog["2026-07-04"] = 13.0   # 신규 거래일
    df2 = store.fetch_prices(uni)  # 증분(기본)

    assert calls[-1] == "20260703"  # 증분은 마지막 저장일부터
    closes = dict(zip(df2["date"], df2["close"]))
    assert closes["2026-07-03"] == 12.5 and closes["2026-07-04"] == 13.0
    assert len(df2) == 4  # 중복 없이 upsert


def test_incremental_price_refresh_caps_a_long_stale_ticker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_parquet(pd.DataFrame([
        {"date": "2024-01-02", "ticker": "012510", "open": 1, "close": 10, "volume": 1},
    ]), store.PRICES_FILE)
    calls = []
    monkeypatch.setattr(krx, "ohlcv", lambda ticker, start, end: calls.append((start, end)) or [])
    store.fetch_prices([{"ticker": "012510"}])
    floor = datetime.date.today() - datetime.timedelta(days=store.PRICE_INCREMENTAL_BOOTSTRAP_DAYS)
    assert calls == [(floor.strftime("%Y%m%d"), datetime.date.today().strftime("%Y%m%d"))]


def test_kr_interior_gap_repair_requests_only_the_missing_session(tmp_path, monkeypatch):
    """일상 증분은 최신 봉부터, 과거 공백은 정확히 하루만 재조회한다."""
    monkeypatch.chdir(tmp_path)
    uni = [{"ticker": "005930", "name": "삼성"}]
    store._write_json(store.UNIVERSE_HISTORY_FILE, {"2026-07-01": uni})
    store._write_parquet(pd.DataFrame([
        {"date": "2026-07-01", "ticker": "005930", "open": 1, "close": 10, "volume": 1},
        {"date": "2026-07-02", "ticker": "005930", "open": 1, "close": 11, "volume": 1},
        {"date": "2026-07-06", "ticker": "005930", "open": 1, "close": 13, "volume": 1},
    ]), store.PRICES_FILE)
    calls = []

    def fake_ohlcv(ticker, start, end):
        calls.append((ticker, start, end))
        day = "2026-07-03" if start == "20260703" else "2026-07-06"
        return [{"date": day, "open": 1.0, "close": 12.0, "volume": 1.0}]

    monkeypatch.setattr(krx, "ohlcv", fake_ohlcv)
    store.fetch_prices(uni)
    assert calls[0][1] == "20260706"
    result = store.repair_kr_price_gaps(limit=1, as_of="2026-07-06")
    assert result["items"] == [{"ticker": "005930", "date": "2026-07-03", "status": "filled"}]
    assert calls[1] == ("005930", "20260703", "20260703")
    df = store._read_parquet(store.PRICES_FILE)
    assert set(df["date"].astype(str).str[:10]) >= {"2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"}
    assert store._load_kr_price_absent() == {}


def test_fetch_prices_remembers_a_session_the_provider_omits(tmp_path, monkeypatch):
    """제공자가 빈 하루를 주면 이름을 남기고 정기 증분을 과거로 되감지 않는다."""
    monkeypatch.chdir(tmp_path)
    uni = [{"ticker": "005930", "name": "삼성"}]
    store._write_json(store.UNIVERSE_HISTORY_FILE, {"2026-07-01": uni})
    store._write_parquet(pd.DataFrame([
        {"date": "2026-07-01", "ticker": "005930", "open": 1, "close": 10, "volume": 1},
        {"date": "2026-07-02", "ticker": "005930", "open": 1, "close": 11, "volume": 1},
        {"date": "2026-07-06", "ticker": "005930", "open": 1, "close": 13, "volume": 1},
    ]), store.PRICES_FILE)

    def omit(ticker, start, end):
        return [{"date": "2026-07-06", "open": 1.0, "close": 13.0, "volume": 1.0}]

    monkeypatch.setattr(krx, "ohlcv", omit)
    result = store.repair_kr_price_gaps(limit=1, as_of="2026-07-06")
    assert result["items"] == [{"ticker": "005930", "date": "2026-07-03", "status": "empty"}]
    assert store._load_kr_price_absent()["005930"] == {"2026-07-03"}
    calls = []

    def tail(ticker, start, end):
        calls.append(start)
        return [{"date": "2026-07-06", "open": 1.0, "close": 13.5, "volume": 1.0}]

    monkeypatch.setattr(krx, "ohlcv", tail)
    store.fetch_prices(uni)
    assert calls == ["20260706"]
    assert store.repair_kr_price_gaps(limit=1, as_of="2026-07-06")["requested"] == 0


def test_gap_repair_keeps_a_past_constituent_without_a_close(tmp_path, monkeypatch):
    """편출 종목도 당시 PIT 구성에 있었다면 공백 재조회 대상이다."""
    monkeypatch.chdir(tmp_path)
    store._write_json(store.UNIVERSE_HISTORY_FILE, {
        "2026-07-01": [{"ticker": "017960", "name": "한국카본"}],
    })
    store._write_parquet(pd.DataFrame([
        {"date": "2026-07-06", "ticker": "017960", "open": 1, "close": 13, "volume": 1},
    ]), store.PRICES_FILE)
    calls = []

    def fake_ohlcv(ticker, start, end):
        calls.append((ticker, start))
        return [{"date": "2026-07-02", "open": 1.0, "close": 12.0, "volume": 1.0}]

    monkeypatch.setattr(krx, "ohlcv", fake_ohlcv)
    store.repair_kr_price_gaps(limit=1, as_of="2026-07-06")
    assert calls == [("017960", "20260702")]


def test_gap_repair_does_not_use_a_different_day_or_guess_auth_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ticker = "012510"
    store._write_json(store.UNIVERSE_HISTORY_FILE, {"2026-07-01": [{"ticker": ticker}]})
    store._write_parquet(pd.DataFrame([
        {"date": "2026-07-01", "ticker": ticker, "open": 1, "close": 10, "volume": 1},
        {"date": "2026-07-06", "ticker": ticker, "open": 1, "close": 11, "volume": 1},
    ]), store.PRICES_FILE)
    def wrong_day(t, start, end):
        return [{"date": "2026-07-06", "open": 1, "close": 11, "volume": 1}]
    monkeypatch.setattr(krx, "ohlcv", wrong_day)
    first = store.repair_kr_price_gaps(limit=1, as_of="2026-07-06")
    assert first["items"] == [{"ticker": ticker, "date": "2026-07-02", "status": "empty"}]
    assert (ticker, "2026-07-02") not in set(zip(
        store._read_parquet(store.PRICES_FILE)["ticker"],
        store._read_parquet(store.PRICES_FILE)["date"]))
    def auth_error(t, start, end):
        raise RuntimeError("KRX 로그인 실패")
    monkeypatch.setattr(krx, "ohlcv", auth_error)
    second = store.repair_kr_price_gaps(limit=1, as_of="2026-07-13")
    assert second["items"] == [{"ticker": ticker, "date": "2026-07-02", "status": "auth_error"}]
    assert store._load_kr_price_absent()[ticker] == {"2026-07-02"}


def test_prices_universe_keeps_a_name_that_left_the_live_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.UNIVERSE_FILE, [{"ticker": "005930", "name": "삼성"}])
    store._write_json(store.UNIVERSE_HISTORY_FILE, {
        "2026-07-01": [{"ticker": "017960", "name": "한국카본"}],
    })
    tickers = {u["ticker"] for u in store.prices_universe()}
    assert tickers == {"005930", "017960"}
