"""매매 입력의 갱신·보존·표시가 파일 쓰기 시각에 속지 않는지 검증한다."""

import datetime
import time

from signal_desk import api, kb, store
from signal_desk.ingest import alphavantage, dart, edgar, fred, krx_open_api


def test_annual_report_year_waits_until_april():
    assert store.latest_annual_report_year(datetime.date(2026, 3, 31)) == 2024
    assert store.latest_annual_report_year(datetime.date(2026, 4, 1)) == 2025


def test_dart_failure_preserves_prior_fundamentals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = {"005930": {"roe": 8.0, "fiscal_year": 2024, "dps": 1400}}
    store._write_json(store.FUNDAMENTALS_FILE, old)
    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "corp"})
    monkeypatch.setattr(dart, "fundamentals", lambda *args: {})
    monkeypatch.setattr(krx_open_api, "market_caps", lambda: {})
    assert store.fetch_fundamentals([{"ticker": "005930"}], bsns_year="2025") == old
    assert store.load_fundamentals() == old


def test_legacy_fundamentals_are_not_called_fresh_by_recent_file_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.FUNDAMENTALS_FILE, {"005930": {"roe": 8.0}})
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.db, "kv_get", lambda key: datetime.date.today().isoformat())
    assert api._dart_stale() is True
    entry = next(e for e in store.data_freshness() if e["key"] == "fundamentals")
    assert entry["stale"] is True and "사업연도 미기록" in entry["note"]


def test_dart_same_year_keeps_dividend_but_new_year_does_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.FUNDAMENTALS_FILE, {
        "005930": {"roe": 8.0, "fiscal_year": 2024, "dps": 1400},
        "000660": {"roe": 4.0, "fiscal_year": 2024, "dps": 900},
    })
    monkeypatch.setattr(dart, "corp_codes", lambda: {"005930": "a", "000660": "b"})
    monkeypatch.setattr(dart, "fundamentals", lambda *args: {"roe": 11.0})
    monkeypatch.setattr(krx_open_api, "market_caps", lambda: {})
    result = store.fetch_fundamentals([{"ticker": "005930"}], bsns_year="2024")
    assert result["005930"]["dps"] == 1400
    assert result["000660"]["dps"] == 900  # 부분 갱신이 다른 종목을 지우지 않음
    result = store.fetch_fundamentals([{"ticker": "005930"}], bsns_year="2025")
    assert "dps" not in result["005930"]
    assert result["000660"]["dps"] == 900


def test_partial_macro_response_keeps_unreturned_series(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.MACRO_FILE, [{"key": "VIXCLS", "value": 21, "asof": "2026-09-25"}])
    monkeypatch.setattr(fred, "macro_indicators", lambda: [{"key": "CPIAUCSL", "value": 2.3,
                                                               "asof": "2026-09-01"}])
    assert len(store.fetch_macro()) == 1
    assert {r["key"] for r in store.load_macro()} == {"VIXCLS", "CPIAUCSL"}
    monkeypatch.setattr(fred, "macro_indicators", lambda: [])
    assert store.fetch_macro() == []
    assert len(store.load_macro()) == 2


def test_macro_freshness_uses_source_date_not_file_write_time(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.MACRO_FILE, [{"key": "VIXCLS", "label": "VIX", "value": 21,
                                         "asof": "2020-01-01"}])
    entry = next(e for e in store.data_freshness() if e["key"] == "macro")
    assert entry["age_hours"] < 1 and entry["stale"] is True
    assert "원천 지연: VIX" in entry["note"]
    assert entry["source_coverage"] == "1/6" and "원천 누락" in entry["note"]


def test_edgar_failed_retry_preserves_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.US_FUNDAMENTALS_FILE, {"AAPL": {
        "net_income": 100, "dps": 1.2, "edgar_observed_at": "2025-01-01"}})
    calls = []
    monkeypatch.setattr(edgar, "fundamentals", lambda ticker: calls.append(ticker) or {})
    assert store.fetch_us_fundamentals_edgar(["AAPL"]) == 1
    assert store.fetch_us_fundamentals_edgar(["AAPL"]) == 0
    saved = store.load_us_fundamentals()["AAPL"]
    assert calls == ["AAPL"] and saved["net_income"] == 100 and saved["dps"] == 1.2
    assert saved["edgar_attempted_at"] == datetime.date.today().isoformat()


def test_edgar_attempt_does_not_block_av_shares_or_erase_edgar_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store._write_json(store.US_FUNDAMENTALS_FILE, {"AAPL": {
        "net_income": 100, "dps": 1.2, "edgar_attempted_at": "2026-09-20"}})
    monkeypatch.setattr(alphavantage, "overview", lambda ticker: {
        "shares": 1000, "per": 12, "sector": "Technology"})
    assert store.fetch_us_fundamentals(["AAPL"]) == 1
    saved = store.load_us_fundamentals()["AAPL"]
    assert saved["shares"] == 1000 and saved["net_income"] == 100
    assert saved["dps"] == 1.2 and saved["edgar_attempted_at"] == "2026-09-20"


def test_advisor_digest_rejects_old_unknown_or_nonfinite(monkeypatch):
    now = time.time()
    holder = {"summary": "old news", "newest_ts": now - 4 * 86400}
    monkeypatch.setattr(kb.db, "kb_digest_get", lambda _: holder)
    assert kb.advisor_digest("005930", now=now) is None
    holder["newest_ts"] = None
    assert kb.advisor_digest("005930", now=now) is None
    holder["newest_ts"] = float("nan")
    assert kb.advisor_digest("005930", now=now) is None
    holder["newest_ts"] = now - 60
    assert kb.advisor_digest("005930", now=now) == holder


def test_timed_source_records_failure_and_respects_interval(monkeypatch):
    kv = {}
    notes = []
    calls = []
    monkeypatch.setattr(api.db, "kv_get", lambda key: kv.get(key))
    monkeypatch.setattr(api.db, "kv_set", lambda key, value: kv.__setitem__(key, value))
    monkeypatch.setattr(api, "_auto_refresh_note", lambda *args: notes.append(args))
    fn = lambda: calls.append(1) or []
    assert api._refresh_timed_source("macro", "FRED", fn, interval=3600) is False
    assert api._refresh_timed_source("macro", "FRED", fn, interval=3600) is False
    assert len(calls) == 1 and notes[-1][2] == "ValueError"
    kv["source_attempt:macro"] = 0
    assert api._refresh_timed_source("macro", "FRED", lambda: [1], interval=3600) is True
    assert notes[-1][2] is None
