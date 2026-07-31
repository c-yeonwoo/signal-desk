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


def test_raising_the_target_depth_unlatches_the_backfill(tmp_path, monkeypatch):
    """완료 플래그가 이미 찍혀 있어도, 이력이 목표에 못 미치면 다시 전량 백필한다."""
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
    assert calls == [True]


@pytest.fixture
def _quiet_maintenance(monkeypatch):
    """시세 갱신만 남기고 나머지 마감후 작업은 무음 처리."""
    monkeypatch.setattr(api, "kb", type("_", (), {"refresh": staticmethod(lambda t: None)}))
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api, "_signals", type("_", (), {"cache_clear": staticmethod(lambda: None),
                                                        "__call__": staticmethod(lambda: [])})())
    monkeypatch.setattr(api, "_regime", type("_", (), {"cache_clear": staticmethod(lambda: None)}))
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda batch=0: {"filled": 0, "stale": 0})
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
    seen: list[int] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: None)
    monkeypatch.setattr(api, "_refresh_us_prices_stale",
                        lambda batch=0: (seen.append(batch), {"filled": 3, "stale": 0})[1])
    api._daily_maintenance([])
    assert seen == [0]                                 # batch=0 → stale 전량


def test_daily_maintenance_backfills_when_history_is_short(monkeypatch, _quiet_maintenance):
    calls: list[bool] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: True)
    monkeypatch.setattr(api.store, "fetch_prices", lambda u, full=False: calls.append(full))
    api._daily_maintenance([])
    assert calls == [True]


def test_price_failure_does_not_block_the_rest_of_maintenance(monkeypatch, _quiet_maintenance):
    done: list[str] = []
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api.store, "fetch_prices",
                        lambda u, full=False: (_ for _ in ()).throw(RuntimeError("krx down")))
    monkeypatch.setattr(api.store, "fetch_short", lambda *a, **k: done.append("short"))
    api._daily_maintenance([])
    assert done == ["short"]                           # 시세가 죽어도 나머지는 계속


def test_maintenance_runs_after_the_close_on_weekdays(monkeypatch):
    """게이트: 평일 마감후 + 그날 아직 안 돎 일 때만 돈다."""
    ran: list[str] = []
    monkeypatch.setattr(api, "_daily_kb_collect", lambda: None)
    monkeypatch.setattr(api, "_morning_digest", lambda: False)
    monkeypatch.setattr(api.db, "user_bots_enabled", lambda: [])
    monkeypatch.setattr(api, "_open_markets", lambda: [])
    monkeypatch.setattr(api, "_backfill_us_prices_batch", lambda n: {"filled": 0, "missing": 0})
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda n: {"filled": 0, "stale": 0})
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
