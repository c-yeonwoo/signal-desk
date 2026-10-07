"""장중 실시간가 오버레이 — 종가열 끝에 잠정봉 1개 append, 종가·날짜 정합, 장외 폴백."""

from pathlib import Path
import shutil
import subprocess

import pandas as pd
import pytest

from signal_desk import store


def _write_prices(tmp_path):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    df = pd.DataFrame([
        {"ticker": "AAA", "date": "2026-07-01", "close": 100.0, "volume": 10},
        {"ticker": "AAA", "date": "2026-07-02", "close": 110.0, "volume": 12},
        {"ticker": "BBB", "date": "2026-07-01", "close": 50.0, "volume": 5},
        {"ticker": "BBB", "date": "2026-07-02", "close": 55.0, "volume": 6},
    ])
    df.to_parquet(cache / "prices.parquet")


def test_overlay_appends_provisional_bar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    base = store.load_price_series()
    assert base["AAA"] == [100.0, 110.0]

    store.set_live_quotes({"AAA": 121.0})  # 장중 현재가
    try:
        s = store.load_price_series()
        assert s["AAA"] == [100.0, 110.0, 121.0]      # 잠정봉 append
        assert s["BBB"] == [50.0, 55.0]               # 라이브 없는 종목은 그대로
        # 날짜열도 +1로 정합(백테스트 date-close 짝 유지)
        d = store.load_dates_by_ticker()
        assert len(d["AAA"]) == len(s["AAA"]) and len(d["BBB"]) == len(s["BBB"])
        # 현재가 표시: price=live, 전일=마지막 종가
        q = store.load_quotes()
        assert q["AAA"]["price"] == 121.0 and q["AAA"]["prev_close"] == 110.0
        assert round(q["AAA"]["change_pct"], 2) == 10.0
    finally:
        store.clear_live_quotes()

    assert store.load_price_series()["AAA"] == [100.0, 110.0]  # 해제 시 종가 복귀


def test_stale_live_observation_is_not_used_for_signal_or_quote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    store.set_live_quotes({"AAA": 121.0})
    try:
        store._LIVE_QUOTE_TS["AAA"] -= 601
        assert store.load_price_series()["AAA"] == [100.0, 110.0]
        assert store.load_quotes()["AAA"]["price"] == 110.0
        assert store.live_price_evidence("AAA")["fresh"] is False
        store._LIVE_QUOTE_TS["AAA"] = store.time.time() + 301
        assert store.load_price_series()["AAA"] == [100.0, 110.0]
    finally:
        store.clear_live_quotes()


def test_quote_failure_falls_back_to_close(tmp_path, monkeypatch):
    """시세 조회가 실패했는데 오버레이를 남기면 낡은 장중가가 계속 시그널·체결가로 쓰인다.
    '오래된 종가'는 정직한 상태지만 '고정된 장중가'는 조용한 거짓말이다."""
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    from signal_desk import api
    from signal_desk.ingest import toss

    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "AAA"}])
    store.set_live_quotes({"AAA": 121.0})
    try:
        monkeypatch.setattr(toss, "price_observations", lambda syms: (_ for _ in ()).throw(RuntimeError("429")))
        api._refresh_live_quotes(["kr"])
        assert store.load_price_series()["AAA"] == [100.0, 110.0]   # 종가로 복귀

        store.set_live_quotes({"AAA": 121.0})
        monkeypatch.setattr(toss, "price_observations", lambda syms: {})        # 빈 응답도 같다
        api._refresh_live_quotes(["kr"])
        assert store.load_price_series()["AAA"] == [100.0, 110.0]
    finally:
        store.clear_live_quotes()


def test_pit_snapshot_is_taken_on_closes(tmp_path, monkeypatch):
    """PIT 점수는 종가 기준이어야 한다 — 채점(accuracy)이 종가로 하는데 스냅샷이 장중가면
    같은 날짜에 두 기준이 섞여 실측이 오염된다."""
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    from signal_desk import api

    seen: list[list[float]] = []
    dates: list[str] = []
    monkeypatch.setattr(api, "kb", type("_", (), {"refresh": staticmethod(lambda t: None)}))
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api, "_regime", type("_", (), {"cache_clear": staticmethod(lambda: None)}))
    monkeypatch.setattr(api, "_signals", type("_", (), {"cache_clear": staticmethod(lambda: None),
                                                       "__call__": staticmethod(lambda: [])})())
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda **kwargs: {"filled": 0, "stale": 0})
    monkeypatch.setattr(api, "_clear_us_signal_caches", lambda: None)
    for name in ("fetch_prices", "fetch_flows", "fetch_market_flow", "fetch_short",
                 "fetch_consensus", "fetch_warnings", "load_universe", "us_price_deferred_tickers"):
        monkeypatch.setattr(api.store, name, lambda *a, **k: [])
    monkeypatch.setattr(api.climate, "snapshot_shadow", lambda s: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)
    # 스냅샷이 불리는 순간의 가격열 — 잠정봉이 남아 있으면 장중가 점수가 저장된다는 뜻
    monkeypatch.setattr(api.store, "snapshot_signals",
                        lambda s, date=None: (seen.append(store.load_price_series()["AAA"]),
                                              dates.append(date), 0)[-1])

    store.set_live_quotes({"AAA": 121.0})
    try:
        api._daily_maintenance([])
    finally:
        store.clear_live_quotes()
    assert seen == [[100.0, 110.0]]                 # 스냅샷 직전 오버레이가 걷혔다
    assert dates == [api._kst_today()]              # 거래일은 KST 기준


def test_overlay_ignores_bad_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    store.set_live_quotes({"AAA": 0, "BBB": None, "CCC": "x", "DDD": float("inf")})  # 유한한 양수만 반영
    try:
        s = store.load_price_series()
        assert s["AAA"] == [100.0, 110.0] and s["BBB"] == [50.0, 55.0]  # 무효값 → 오버레이 없음
    finally:
        store.clear_live_quotes()


def test_held_quote_merge_keeps_other_universe_prices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    store.set_live_quotes({"AAA": 121.0, "BBB": 60.0})
    try:
        before = store.live_quotes_snapshot()["revision"]
        store.merge_live_quotes({"AAA": 125.0})
        snapshot = store.live_quotes_snapshot()
        assert snapshot["revision"] == before + 1
        assert snapshot["quotes"] == {"AAA": 125.0, "BBB": 60.0}
        q = store.load_quotes()
        assert q["AAA"]["price"] == 125.0
        assert q["BBB"]["price"] == 60.0
    finally:
        store.clear_live_quotes()


def test_merge_does_not_refresh_unrelated_quote_age():
    from signal_desk import api

    store.set_live_quotes({"AAA": 121.0, "BBB": 60.0})
    try:
        old_bbb = store.live_quote_updated("BBB") - 900
        store._LIVE_QUOTE_TS["BBB"] = old_bbb
        store.merge_live_quotes({"AAA": 125.0})
        assert store.live_quote_updated("AAA") > old_bbb
        assert store.live_quote_updated("BBB") == old_bbb
        assert store.live_quotes_snapshot()["quote_updated"]["BBB"] == old_bbb
        assert store.live_status()["updated"] > old_bbb  # 전역 성공 시각은 종목별 신선도가 아니다.
        assert api._chart_freshness(["2026-10-06"], ticker="AAA")["live_updated"] != (
            api._chart_freshness(["2026-10-06"], ticker="BBB")["live_updated"])
        assert api._chart_freshness(["2026-10-06"], ticker="BBB")["live_stale"] is True
        assert api._chart_freshness(["2026-10-06"], ticker="AAA")["live_on"] is True
        assert api._chart_freshness(["2026-10-06"])["live_on"] is False
    finally:
        store.clear_live_quotes()
    assert store.live_quote_updated("BBB") is None


def test_live_quote_snapshot_keeps_source_timestamp_separate_from_receive_time():
    source = {
        "price": 121.0, "provider": "toss", "price_kind": "last",
        "source_timestamp": "2026-10-06T13:00:00+09:00",
        "source_timestamp_parsed_utc": "2026-10-06T04:00:00+00:00",
        "source_time_verified": False,
    }
    store.set_live_quotes({"AAA": source})
    try:
        snapshot = store.live_quotes_snapshot()
        assert snapshot["quotes"]["AAA"] == 121.0
        assert snapshot["quote_updated"]["AAA"] == store.live_quote_updated("AAA")
        assert all(snapshot["quote_meta"]["AAA"][key] == value for key, value in source.items())
        assert snapshot["quote_meta"]["AAA"]["observation_id"]
        assert snapshot["quote_meta"]["AAA"]["received_at"] == snapshot["quote_updated"]["AAA"]
        status = store.live_status()
        assert status["source_time_present"] == 1
        assert status["source_time_verified"] == 0
    finally:
        store.clear_live_quotes()


def test_browser_refuses_an_old_sse_quote():
    if not shutil.which("node"):
        pytest.skip("Node is needed for the live quote renderer")
    script = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
const code=html.slice(html.indexOf('function liveQuoteFresh('),html.indexOf('// 조회 전용 비교 상태',html.indexOf('function liveQuoteFresh(')));
const el={textContent:'',style:{}};
const td={children:[],querySelector:()=>el};
const tr={dataset:{ticker:'AAA'},children:[null,td]};
const ctx=vm.createContext({Date,Number,Object,document:{querySelectorAll:()=>[tr],getElementById:()=>null},
  _liveQuoteTimes:{AAA:Date.now()/1000-901},_liveQuotePrices:{AAA:121},_sigMarket:'kospi',fmtNum:v=>String(v)});
vm.runInContext(code,ctx);
ctx.paintLiveQuotePrices();
assert.equal(el.textContent,'현재가 갱신 지연');
ctx._liveQuoteTimes.AAA=Date.now()/1000-60;
ctx.paintLiveQuotePrices();
assert.equal(el.textContent,'현재가 121원');
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_live_status_ui_does_not_call_partial_or_other_market_quotes_healthy():
    if not shutil.which("node"):
        pytest.skip("Node is needed for the live status renderer")
    script = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
const code=html.slice(html.indexOf('function liveStatusView('),html.indexOf('async function loadLiveStatus('));
const ctx=vm.createContext({Date,Number,Object});
vm.runInContext(code,ctx);
const now=Date.now()/1000;
const d={toss:true,kr_open:false,us_open:true,on:true,count:500,fresh_count:500,
  updated:now,attempt_ts:now,attempt_result:'ok',
  coverage:{us:{requested_count:509,received_count:500,missing_count:9,missing_sample:['AAPL']}}};
assert.match(ctx.liveStatusView(d,'us').text,/500\/509종목 수신 · 9종목 미수신/);
assert.match(ctx.liveStatusView(d,'kr').text,/이 시장은 장외/);
assert.match(ctx.liveStatusView({...d,attempt_ts:now-601},'us').text,/전체 현재가 확인 지연/);
assert.match(ctx.liveStatusView({...d,coverage:{us:{requested_count:509,received_count:509,missing_count:0,missing_sample:[]}}},'us').text,/509\/509종목 수신/);
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
