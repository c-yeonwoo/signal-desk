"""Market-session gated private summaries and single-episode weekly comparisons."""

import datetime as dt
from zoneinfo import ZoneInfo

from signal_desk import api, config, db, telegram_inbound


def test_us_snapshot_waits_for_exact_completed_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "uids_with_holdings", lambda: [7])
    monkeypatch.setattr(db, "holdings_list", lambda _uid: [{"ticker": "AAPL"}])
    monkeypatch.setattr(api, "_holdings_by_market", lambda rows, market: rows if market == "us" else [])
    state = {"as_of": "2026-09-24"}
    def analysis(_uid, _market):
        return {"as_of": state["as_of"], "audit": {"timing": {"aligned": True}},
                "summary": {"total_value": 100.0}, "data_quality": {"status": "complete"},
                "guidance": []}
    monkeypatch.setattr(api, "_portfolio_analysis", analysis)
    assert api._snapshot_personal_portfolios_market("us", expected_session="2026-09-25") == 0
    assert db.portfolio_snapshot_latest(7, "us") is None
    state["as_of"] = "2026-09-25"
    assert api._snapshot_personal_portfolios_market("us", expected_session="2026-09-25") == 1
    assert api._snapshot_personal_portfolios_market("us", expected_session="2026-09-25") == 0
    assert db.portfolio_snapshot_for_session(7, "us", as_of="2026-09-25", source="daily_close")


def test_us_digest_uses_independent_completed_session_gate(monkeypatch):
    kst = ZoneInfo("Asia/Seoul")
    calls = []
    monkeypatch.setattr(api.market_clock, "is_open", lambda *_a: False)
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda *_a: "2026-09-25")
    monkeypatch.setattr(api.db, "kv_get", lambda _key: "2026-09-25")
    monkeypatch.setattr(api, "_snapshot_personal_portfolios_market",
                        lambda market, **kwargs: calls.append((market, kwargs["expected_session"])))
    monkeypatch.setattr(api.telegram_inbound, "enqueue_daily_summaries",
                        lambda session, market: calls.append((market, session)))
    assert not api._maybe_us_personal_close(dt.datetime(2026, 9, 26, 14, tzinfo=kst))
    assert not api._maybe_us_personal_close(dt.datetime(2026, 9, 26, 16))  # naive clock fails closed
    assert api._maybe_us_personal_close(dt.datetime(2026, 9, 26, 16, tzinfo=kst))
    assert calls == [("us", "2026-09-25"), ("us", "2026-09-25")]
    monkeypatch.setattr(api.db, "kv_get", lambda _key: "2026-09-24")
    assert not api._maybe_us_personal_close(dt.datetime(2026, 9, 26, 17, tzinfo=kst))


def test_daily_digest_reads_close_not_newer_user_requested_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "toss_account_owner", lambda: None)
    db.portfolio_snapshot_add(7, "us", as_of="2026-09-25", source="daily_close",
                              total_value=100, data_quality="complete",
                              payload={"guidance": [{"action": "분산 확인", "reason": "마감 근거"}]})
    db.portfolio_snapshot_add(7, "us", as_of="2026-09-25", source="user_requested",
                              total_value=101, data_quality="partial",
                              payload={"guidance": [{"action": "임시 화면", "reason": "장중 근거"}]})
    text = telegram_inbound.daily_summary(7, "us", "2026-09-25")
    assert "마감 근거" in text and "장중 근거" not in text


def test_weekly_shadow_uses_one_completed_recent_episode_not_sum(monkeypatch):
    monkeypatch.setattr(db, "portfolio_artifact_ids", lambda *_a, **_k: ["old", "recent"])
    results = {
        "old": {"ready": True, "complete": True, "mode": "shadow", "as_of": "2026-09-19",
                "completed_sessions": 20, "attribution": {"gross_vs_hold_pp": 10,
                "incremental_cost_drag_pp": 2, "net_vs_hold_pp": 8}},
        "recent": {"ready": True, "complete": True, "mode": "shadow", "as_of": "2026-09-25",
                   "completed_sessions": 20, "attribution": {"gross_vs_hold_pp": 1.5,
                   "incremental_cost_drag_pp": 0.4, "net_vs_hold_pp": 1.1}},
    }
    monkeypatch.setattr(db, "portfolio_comparison_latest",
                        lambda _uid, _market, aid: {"result": results[aid]})
    text = telegram_inbound.weekly_shadow_summary(7, "kr", today=dt.date(2026, 9, 27))
    assert "2026-09-25" in text and "+0.40%p" in text and "+1.10%p" in text
    assert "+10.00%p" not in text and "실제 주문·실계좌 수익이 아닙니다" in text
    results["recent"]["complete"] = False
    assert telegram_inbound.weekly_shadow_summary(7, "kr", today=dt.date(2026, 9, 27)) is None


def test_weekly_opt_in_and_sunday_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "telegram_links_all", lambda: [
        {"uid": 7, "markets": ["kr"], "alert_types": ["weekly_summary"]},
        {"uid": 8, "markets": ["kr"], "alert_types": []},
    ])
    monkeypatch.setattr(telegram_inbound, "weekly_shadow_summary",
                        lambda uid, market, **_k: "verified" if uid == 7 and market == "kr" else None)
    calls = []
    monkeypatch.setattr(telegram_inbound.notify, "enqueue_user",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    kst = ZoneInfo("Asia/Seoul")
    assert telegram_inbound.enqueue_weekly_summaries(now=dt.datetime(2026, 9, 26, 19, tzinfo=kst)) == 0
    assert telegram_inbound.enqueue_weekly_summaries(now=dt.datetime(2026, 9, 27, 17, tzinfo=kst)) == 0
    assert telegram_inbound.enqueue_weekly_summaries(now=dt.datetime(2026, 9, 27, 18, tzinfo=kst)) == 1
    assert len(calls) == 1 and calls[0][0][0] == 7
    assert calls[0][1]["alert_type"] == "weekly_summary"
