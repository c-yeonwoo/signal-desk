"""유저별 예약 주문 실행 — paper 계좌 기준."""

import json

from signal_desk import bot, db, store

UID = 6


def _setup(monkeypatch, tmp_path, prices):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "load_price_series", lambda: prices)
    monkeypatch.setattr(store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "AAA", "name": "가"}])
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 100_000.0, "positions": {}}))


def test_execute_reservation_fills_within_chase(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, {"AAA": [100.0, 101.0]})  # 현재가 101
    db.bot_reservation_add(UID, "AAA", "가", "buy", 100.0, 0.02, "테스트")  # 상한 102
    out = bot.execute_reservations(UID)
    assert out["executed"][0]["status"] == "filled"          # 101 ≤ 102 → 체결
    assert db.bot_reservations_pending(UID) == []
    assert db.bot_position_get(UID, "AAA")["qty"] >= 1        # paper에 반영


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
