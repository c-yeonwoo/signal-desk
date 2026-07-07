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
