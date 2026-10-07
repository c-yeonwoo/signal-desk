"""실시간가 갱신 시도 기록 — 성공/실패 무관하게 마지막 시도 시각·결과를 남긴다(진단용)."""

from signal_desk import store


def test_live_status_records_attempt():
    store.clear_live_quotes()
    store.note_live_attempt("no_quotes", ["us"])
    s = store.live_status()
    assert s["on"] is False and s["updated"] is None       # 성공 갱신은 없음
    assert s["attempt_result"] == "no_quotes" and s["attempt_markets"] == ["us"]
    assert s["attempt_ts"] is not None                      # 시도 시각은 찍힘

    store.set_live_quotes({"AAPL": 200.0})
    store.note_live_attempt("ok", ["us"])
    s2 = store.live_status()
    assert s2["on"] and s2["updated"] and s2["attempt_result"] == "ok"
    store.clear_live_quotes()


def test_live_attempt_coverage_keeps_missing_symbols_separate_from_fresh_quotes():
    requested = {"us": {"AAPL", "MSFT", "NVDA"}}
    store.set_live_quotes({"AAPL": 200.0})
    try:
        store.note_live_attempt("ok", ["us"], requested_by_market=requested,
                                received_by_market={"us": {"AAPL", "UNKNOWN"}})
        status = store.live_status()
        assert status["fresh_count"] == 1
        assert status["coverage"]["us"] == {
            "requested_count": 3, "received_count": 1, "missing_count": 2,
            "missing_sample": ["MSFT", "NVDA"]}

        # 보유 종목만 새로 받아도 직전 전체 조회의 분모를 바꾸지 않는다.
        store.merge_live_quotes({"NVDA": 150.0})
        assert store.live_status()["coverage"]["us"]["received_count"] == 1

        store.note_live_attempt("closed")
        assert store.live_status()["coverage"] == {}
    finally:
        store.clear_live_quotes()
