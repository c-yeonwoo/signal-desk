"""시세 이력이 스스로 채워지고(깊이) 스스로 갱신되는지(신선도).

실제 사고: 일일 루프가 일봉을 한 번도 갱신하지 않아 시세는 7/3에 멈춘 채 시그널만 7/24까지
쌓였고, 완료 플래그가 래치로 걸려 목표를 5년으로 올린 뒤에도 400일치에 머물렀다."""

import datetime

import pandas as pd
import pytest

from signal_desk import api, store


def _seed_prices(tmp_path, depth_days: int, tickers=("005930", "000660")) -> None:
    (tmp_path / "data/cache").mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    rows = [{"date": (today - datetime.timedelta(days=d)).isoformat(), "ticker": t,
             "open": 100.0, "close": 100.0, "volume": 1}
            for t in tickers for d in (0, depth_days)]
    pd.DataFrame(rows).to_parquet(store.PRICES_FILE, index=False)


def test_us_freshness_names_stale_tickers_with_a_bounded_preview(tmp_path, monkeypatch):
    """전체 지연 수뿐 아니라 조사 대상 종목을 운영 진단에서 바로 식별한다."""
    path = tmp_path / "us_prices.parquet"
    path.touch()
    monkeypatch.setattr(store, "US_PRICES_FILE", path)
    tickers = [f"T{i:02d}" for i in range(12)]
    monkeypatch.setattr(store, "us_price_last_dates", lambda: {t: "2026-10-02" for t in tickers})
    monkeypatch.setattr(store, "us_prices_stale_tickers", lambda _tickers: list(reversed(tickers)))
    monkeypatch.setattr(store, "us_unconfirmed_price_tickers", lambda tickers=None: [])
    monkeypatch.setattr(store, "us_expected_last_bar", lambda: "2026-10-05")
    monkeypatch.setattr(store, "us_missing_trading_days", lambda *_args: [])
    monkeypatch.setattr(store, "us_price_holes", lambda: {"ready": True, "holes_total": 0})

    freshness = store._us_prices_freshness()

    assert freshness["rows"] == 12
    assert freshness["stale_tickers"] == [f"T{i:02d}" for i in range(10)]
    assert freshness["stale_tickers_omitted"] == 2
    assert "12/12종목 갱신 대상(T00, T01" in freshness["note"]
    assert "외 2종목" in freshness["note"]


def test_missing_cache_asks_for_a_full_backfill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert store.prices_depth_days() == 0
    assert store.prices_need_deep_backfill() is True


def test_shallow_history_asks_for_a_full_backfill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, depth_days=400)          # 실제로 쌓여 있던 깊이
    assert store.prices_depth_days() == 400
    assert store.prices_need_deep_backfill() is True


def test_deep_history_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, depth_days=store.PRICE_HISTORY_DAYS)
    assert store.prices_need_deep_backfill() is False   # 채워졌으면 매일 재백필하지 않는다


def test_refresh_with_shallow_history_does_not_start_full_backfill(tmp_path, monkeypatch):
    """얕은 이력은 상태로 남기되 일반 갱신에서 5년 전량 수집을 시작하지 않는다."""
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, depth_days=400)
    kv = {"prices_deep_backfilled": "2025-06-01"}       # 예전에 한 번 백필했다고 표시됨
    calls: list[bool] = []
    monkeypatch.setattr(api.store, "fetch_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: calls.append(full))
    monkeypatch.setattr(api, "_dart_stale", lambda: False)
    monkeypatch.setattr(api.store, "update_valuation", lambda: None)
    monkeypatch.setattr(api.store, "load_fundamentals", lambda: {"005930": {}})
    monkeypatch.setattr(api.store, "load_company_profiles", lambda: {"005930": {"ceo": "x"}})
    monkeypatch.setattr(api.db, "kv_get", lambda k: kv.get(k))
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: kv.__setitem__(k, v))
    api._refresh_kr({})
    assert calls == [False]


def test_explicit_full_flag_is_required_for_full_price_refresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(api.store, "fetch_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "prices_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: calls.append(full))
    monkeypatch.setattr(api, "_dart_stale", lambda: False)
    monkeypatch.setattr(api.store, "update_valuation", lambda: None)
    monkeypatch.setattr(api.store, "load_fundamentals", lambda: {"005930": {}})
    monkeypatch.setattr(api.store, "load_company_profiles", lambda: {"005930": {"ceo": "x"}})
    monkeypatch.setattr(api.db, "kv_get", lambda k: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)
    api._refresh_kr({"full_prices": "false"})
    assert calls == [False]
    api._refresh_kr({"full_prices": True})
    assert calls == [False, True]


@pytest.fixture
def _quiet_maintenance(monkeypatch):
    """시세 갱신만 남기고 나머지 마감후 작업은 무음 처리."""
    monkeypatch.setattr(api, "kb", type("_", (), {"refresh": staticmethod(lambda t: None)}))
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api, "_signals", type("_", (), {"cache_clear": staticmethod(lambda: None),
                                                        "__call__": staticmethod(lambda: [])})())
    monkeypatch.setattr(api, "_regime", type("_", (), {"cache_clear": staticmethod(lambda: None)}))
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda **kwargs: {"filled": 0, "stale": 0})
    monkeypatch.setattr(api.store, "repair_kr_price_gaps", lambda: {"filled": 0})
    monkeypatch.setattr(api, "_clear_us_signal_caches", lambda: None)
    for name in ("fetch_flows", "fetch_market_flow", "fetch_short", "fetch_consensus",
                 "snapshot_signals", "load_universe", "us_price_deferred_tickers"):
        monkeypatch.setattr(api.store, name, lambda *a, **k: [])
    monkeypatch.setattr(api.climate, "snapshot_shadow", lambda s: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)


def test_daily_maintenance_refreshes_prices_without_bot_users(monkeypatch, _quiet_maintenance):
    """봇 사용자가 한 명도 없어도 시세는 갱신돼야 한다 — 데이터 신선도가 봇에 딸리면 안 된다."""
    calls: list[bool] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: calls.append(full))
    api._daily_maintenance([])
    assert calls == [False]                            # 증분 갱신이 돌았다


def test_daily_maintenance_refreshes_stale_us_prices(monkeypatch, _quiet_maintenance):
    """US는 누락 백필만으론 안 된다 — 마감후 루프가 stale 종목을 다시 당겨야 한다."""
    seen: list[dict] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: None)
    monkeypatch.setattr(api.store, "us_expected_last_bar", lambda at: "2026-09-28")
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-09-28")
    monkeypatch.setattr(api, "_refresh_us_prices_stale",
                        lambda **kwargs: (seen.append(kwargs), {"filled": 3, "stale": 0})[1])
    api._daily_maintenance([])
    assert seen == [{"batch": 0, "max_trading_days": 0}]  # 완료 세션의 1일 결손도 재시도


def test_daily_us_refresh_keeps_holiday_slack_when_calendar_disagrees(monkeypatch, _quiet_maintenance):
    seen = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: None)
    monkeypatch.setattr(api.store, "us_expected_last_bar", lambda at: "2026-09-28")
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-09-25")
    monkeypatch.setattr(api, "_refresh_us_prices_stale",
                        lambda **kwargs: (seen.append(kwargs), {"filled": 0, "stale": 0})[1])
    api._daily_maintenance([])
    assert seen == [{"batch": 0, "max_trading_days": 1}]


def test_daily_maintenance_keeps_incremental_when_history_is_short(monkeypatch, _quiet_maintenance):
    calls: list[bool] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: True)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: calls.append(full))
    api._daily_maintenance([])
    assert calls == [False]


def test_price_failure_does_not_block_the_rest_of_maintenance(monkeypatch, _quiet_maintenance):
    done: list[str] = []
    alerts: list[str] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices",
                        lambda u, full=False: (_ for _ in ()).throw(RuntimeError("krx down")))
    monkeypatch.setattr(api, "_check_kr_price_refresh",
                        lambda now, *, reason: alerts.append(reason))
    monkeypatch.setattr(api.store, "fetch_short", lambda *a, **k: done.append("short"))
    api._daily_maintenance([])
    assert done == ["short"]                           # 시세가 죽어도 나머지는 계속
    assert alerts == ["RuntimeError"]                  # 실패를 긴급 알림 점검으로 전달


def test_silent_price_provider_failure_still_checks_last_bars(monkeypatch, _quiet_maintenance):
    """KR 수집기는 개별 종목 실패를 삼키므로 반환 성공을 신선도로 간주하지 않는다."""
    alerts = []
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: None)
    monkeypatch.setattr(api, "_check_kr_price_refresh",
                        lambda now, *, reason: alerts.append(reason))
    api._daily_maintenance([])
    assert alerts == ["재시도 후 최신 종가 없음"]


def test_paper_snapshot_failure_does_not_block_other_ledgers_or_daily_completion(
        monkeypatch, _quiet_maintenance):
    """한 레퍼런스 장부 오류가 다른 장부와 공식 자료 수집의 선행 게이트를 막지 않는다."""
    calls = []
    saved = {}

    def snapshot(uid, market):
        calls.append((uid, market))
        if (uid, market) == (101, "kr"):
            raise RuntimeError("paper ledger unavailable")
        return True

    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: None)
    monkeypatch.setattr(api.bot, "snapshot_positions", snapshot)
    monkeypatch.setattr(api.db, "kv_set", lambda key, value: saved.__setitem__(key, value))
    api._daily_maintenance([101, 102])

    assert calls == [(101, "kr"), (101, "us"), (102, "kr"), (102, "us")]
    assert saved["bot_daily_snap"] == api._kst_today()


def test_maintenance_runs_after_the_close_on_weekdays(monkeypatch):
    """게이트: 평일 마감후 + 그날 아직 안 돎 일 때만 돈다."""
    ran: list[str] = []
    monkeypatch.setattr(api, "_daily_kb_collect", lambda: None)
    monkeypatch.setattr(api, "_morning_digest", lambda: False)
    monkeypatch.setattr(api.db, "user_bots_enabled", lambda: [])
    monkeypatch.setattr(api, "_open_markets", lambda: [])
    monkeypatch.setattr(api, "_backfill_us_prices_batch", lambda n: {"filled": 0, "missing": 0})
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda *a, **k: {"filled": 0, "stale": 0})
    monkeypatch.setattr(api, "_backfill_about_batch", lambda n: 0)
    monkeypatch.setattr(api, "_backfill_moves_batch", lambda n: 0)
    monkeypatch.setattr(api, "_daily_maintenance", lambda enabled: ran.append("ran"))
    monkeypatch.setattr(api.db, "kv_get", lambda k: None)

    def _at(dt: datetime.datetime):
        monkeypatch.setattr(api, "_kst_now", lambda: dt)

    _at(datetime.datetime(2026, 7, 24, 12, 0))   # 금요일 장중
    api._bot_loop_iteration()
    assert ran == []
    _at(datetime.datetime(2026, 7, 25, 16, 0))   # 토요일 마감후
    api._bot_loop_iteration()
    assert ran == []
    _at(datetime.datetime(2026, 7, 24, 16, 0))   # 금요일 마감후
    api._bot_loop_iteration()
    assert ran == ["ran"]


def test_one_session_us_hole_is_retried_when_the_calendar_agrees():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    matched = datetime.datetime(2026, 10, 1, 15, 40, tzinfo=kst)
    assert api._us_price_stale_slack(matched) == 0
    # 2026-09-07은 미국 휴장. 주말만 뺀 기대일(09-07)과 완료 세션(09-04)이 갈라진다.
    holiday = datetime.datetime(2026, 9, 8, 15, 40, tzinfo=kst)
    assert api._us_price_stale_slack(holiday) == 1
