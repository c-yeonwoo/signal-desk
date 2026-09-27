"""유저별 예약 주문 실행 — paper 계좌 기준."""

import json

from signal_desk import bot, db, store
from signal_desk.signals.engine import SignalResult

UID = 6


def _setup(monkeypatch, tmp_path, prices):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "load_price_series", lambda: prices)
    monkeypatch.setattr(store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "AAA", "name": "가"}])
    monkeypatch.setattr(bot, "_market_read", lambda _: {"eff_cfg": None, "context": {"exposure": 1.0}})
    monkeypatch.setattr(bot, "_market_signals", lambda market, mr: (
        [], store.load_us_price_series() if market == "us" else store.load_price_series(),
        [SignalResult(ticker=t, name=t, score=2.0, kind="BUY", confidence=0.5,
                      technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])
         for t in (store.load_us_price_series() if market == "us" else store.load_price_series())], {}))
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 100_000.0, "positions": {}}))


def test_execute_reservation_fills_within_chase(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})  # 현재가 101
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")  # 상한 102
    out = bot.execute_reservations(UID)
    assert out["executed"][0]["status"] == "filled"          # 101 ≤ 102 → 체결
    assert db.bot_reservations_pending(UID) == []
    assert db.bot_position_get(UID, "AAA")["qty"] >= 1        # paper에 반영


def test_kill_switch_blocks_pending_reservation(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")
    monkeypatch.setattr(bot.config, "bot_kill_switch", lambda: True)
    out = bot.execute_reservations(UID)
    assert out["ok"] is False and out["executed"] == []
    assert db.bot_position_get(UID, "AAA") is None
    assert len(db.bot_reservations_pending(UID)) == 1


def test_reservation_rechecks_current_signal(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")
    monkeypatch.setattr(bot, "_market_signals", lambda market, mr: ([], {"AAA": [100.0, 101.0]}, [], {}))
    out = bot.execute_reservations(UID)
    assert out["executed"][0]["status"] == "skipped_signal"
    assert db.bot_position_get(UID, "AAA") is None


def test_reservation_rechecks_exposure_cap(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")
    monkeypatch.setattr(bot, "_market_read", lambda _: {"eff_cfg": None, "context": {"exposure": 0.0}})
    out = bot.execute_reservations(UID)
    assert out["executed"][0]["status"] == "skipped_policy"
    assert db.bot_position_get(UID, "AAA") is None


def test_execute_reservation_skips_when_price_ran_up(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 110.0]})  # 현재가 110(+10%)
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")  # 상한 102
    out = bot.execute_reservations(UID)
    assert out["executed"][0]["status"] == "skipped_price"   # 110 > 102 → 추격 안 함
    assert db.bot_position_get(UID, "AAA") is None


def test_execute_reservations_none_pending(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0]})
    out = bot.execute_reservations(UID)
    assert out["ok"] is True and out["executed"] == []


def test_reservations_isolated_by_market(tmp_path, monkeypatch):
    """국내/해외 예약 격리 — 한 시장 예약이 다른 시장에 안 보이고, 실행도 각자 계좌로."""
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"AAPL": [200.0, 201.0]})
    monkeypatch.setattr(store, "load_us_universe", lambda: [{"ticker": "AAPL", "name": "Apple"}])
    db.kv_set(f"paper_account:{UID}:us", json.dumps({"cash": 100_000.0, "positions": {}}))

    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "국내", market="kr")
    db.bot_reservation_add(UID, "AAPL", "Apple", "buy", 200.0, 0.02, "해외", market="us")

    kr = db.bot_reservations_pending(UID, "kr")
    us = db.bot_reservations_pending(UID, "us")
    assert [r["ticker"] for r in kr] == ["AAA"]      # 국내엔 국내만
    assert [r["ticker"] for r in us] == ["AAPL"]     # 해외엔 해외만

    out = bot.execute_reservations(UID, market="us")
    assert out["market"] == "us" and out["executed"][0]["ticker"] == "AAPL"
    assert db.bot_position_get(UID, "AAPL") is not None                 # us 계좌에 체결
    assert db.bot_reservations_pending(UID, "kr")[0]["ticker"] == "AAA"  # 국내 예약은 그대로 대기
