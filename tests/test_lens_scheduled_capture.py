"""조회 시각이 아닌 고정 마감 창만 연구 표본이 되며 결손·중복은 닫힌다."""

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from signal_desk import api, db
from signal_desk.signals import lens_forward


KST = ZoneInfo("Asia/Seoul")


def test_first_completed_session_window_handles_markets_and_holidays():
    kr = dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST)
    us = dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST)
    assert lens_forward.scheduled_capture_window("kr", kr) == {
        "market": "kr", "session": "2026-10-06", "iso_week": "2026-W41"}
    assert lens_forward.scheduled_capture_window("us", us) == {
        "market": "us", "session": "2026-10-05", "iso_week": "2026-W41"}
    assert lens_forward.scheduled_capture_window("kr", kr.replace(hour=15)) is None
    assert lens_forward.scheduled_capture_window("us", us.replace(hour=21)) is None
    assert lens_forward.scheduled_capture_window("kr", kr + dt.timedelta(days=1)) is None
    assert lens_forward.scheduled_capture_window("kr", dt.datetime(2026, 9, 28, 16, 10, tzinfo=KST)) is None


def test_expected_weeks_count_missed_calendar_windows():
    before = dt.datetime(2026, 10, 6, 20, 59, tzinfo=KST)
    after = before.replace(hour=21, minute=0)
    assert lens_forward.expected_capture_weeks("kr", before) == []
    assert lens_forward.expected_capture_weeks("kr", after) == ["2026-W41"]
    assert lens_forward.expected_capture_weeks("us", after) == ["2026-W41"]


def test_daily_price_mark_window_uses_each_market_close():
    kst_after_close = dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST)
    assert lens_forward.daily_mark_window("kr", kst_after_close)["session"] == "2026-10-06"
    assert lens_forward.daily_mark_window("us", kst_after_close)["session"] == "2026-10-05"
    assert lens_forward.daily_mark_window("us", kst_after_close.replace(hour=21)) is None


def test_scheduled_capture_is_separate_from_user_view_and_immutable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST)
    db.kv_set("bot_daily_snap", "2026-10-06")
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "AAA"}])
    monkeypatch.setattr(api.store, "load_dates_by_ticker", lambda: {"AAA": ["2026-10-06"]})
    monkeypatch.setattr(api.store, "clear_live_quotes", lambda: None)
    monkeypatch.setattr(api.market_clock, "is_open", lambda *args: False)
    called = []

    def response(market, *, observed_at):
        called.append((market, observed_at))
        db.lens_snapshot_put({"id": "fixed-1", "market": "kr", "version": "v",
                              "signal_policy_id": "p", "observed_at": observed_at,
                              "mode": "read_only", "order_eligible": False, "rows": []})
        return {"ready": True, "lens_snapshot": {"id": "fixed-1", "observed_at": observed_at,
                                                   "recorded": True}}

    monkeypatch.setattr(api, "_signal_response", response)
    first = api._maybe_capture_scheduled_lens("kr", now)
    second = api._maybe_capture_scheduled_lens("kr", now + dt.timedelta(minutes=30))
    assert first["status"] == "captured" and second["status"] == "already_frozen"
    assert len(called) == 1
    assert db.lens_scheduled_cohorts("kr")[0]["snapshot_id"] == "fixed-1"
    assert db.lens_forward_cohorts("kr") == []  # 사용자 조회 원장은 별도


def test_stale_prices_cannot_become_a_scheduled_cohort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST)
    db.kv_set("bot_daily_snap", "2026-10-06")
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "AAA"}, {"ticker": "BBB"}])
    monkeypatch.setattr(api.store, "load_dates_by_ticker", lambda: {"AAA": ["2026-10-06"],
                                                                      "BBB": ["2026-10-02"]})
    monkeypatch.setattr(api, "_signal_response", lambda *args, **kwargs: 1 / 0)
    result = api._maybe_capture_scheduled_lens("kr", now)
    assert result["status"] == "blocked_stale_prices"
    assert result["fresh_fraction"] == .5
    assert db.lens_scheduled_cohorts("kr") == []


def test_week_mismatch_and_unwired_loop_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    observed = int(dt.datetime(2026, 10, 6, 16, 10, tzinfo=KST).timestamp())
    db.lens_snapshot_put({"id": "s", "market": "kr", "version": "v", "observed_at": observed,
                          "mode": "read_only", "order_eligible": False, "rows": []})
    with pytest.raises(ValueError, match="week mismatch"):
        db.lens_scheduled_cohort_freeze("kr", "2026-W40", "2026-10-06", "s", observed)
    source = (Path(__file__).resolve().parents[1] / "src/signal_desk/api.py").read_text()
    loop = source.split("def _bot_loop_iteration(", 1)[1].split("\ndef ", 1)[0]
    assert "_maybe_capture_scheduled_lens(mkt, now)" in loop
    assert "lens_forward.collect_price_marks(cohorts, mkt, loader, db.lens_scheduled_price_mark" in loop
    assert "lens_forward.audit_price_revisions(" in loop
    assert "db.lens_scheduled_price_halt_add" in loop
