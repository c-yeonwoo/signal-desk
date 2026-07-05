from signal_desk import db

UID = 5


def test_user_bot_defaults_and_toggle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = db.user_bot_get(UID)
    assert cfg["enabled"] is False and cfg["trading_style"] == "balanced" and cfg["seed_cash"] == 10_000_000

    db.user_bot_set_enabled(UID, True)
    assert db.user_bot_get(UID)["enabled"] is True
    assert db.user_bots_enabled() == [UID]
    db.user_bot_set_enabled(UID, False)
    assert db.user_bots_enabled() == []

    db.user_bot_set_style(UID, "aggressive")
    db.user_bot_set_seed(UID, 5_000_000)
    c = db.user_bot_get(UID)
    assert c["trading_style"] == "aggressive" and c["seed_cash"] == 5_000_000


def test_bot_position_upsert_and_delete_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.bot_positions_all(UID) == []

    db.bot_position_upsert(UID, "005930", "삼성전자", 10, 70000.0, 72000.0, "2026-07-01")
    pos = db.bot_position_get(UID, "005930")
    assert pos == {"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 70000.0,
                   "peak_price": 72000.0, "entry_date": "2026-07-01",
                   "last_price": None, "last_pnl_pct": None}
    # 다른 유저 격리
    assert db.bot_positions_all(99) == []
    db.bot_position_upsert(UID, "005930", "삼성전자", 15, 71000.0, 73000.0, "2026-07-01")
    assert db.bot_position_get(UID, "005930")["qty"] == 15

    db.bot_position_delete(UID, "005930")
    assert db.bot_position_get(UID, "005930") is None


def test_bot_trade_log_and_recent_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_trade_log(UID, "005930", "삼성전자", "buy", 10, 70000.0, "SIGNAL", "ORD1")
    db.bot_trade_log(UID, "000660", "SK하이닉스", "sell", 5, 200000.0, "STOP_LOSS", "ORD2")
    db.bot_trade_log(99, "035720", "카카오", "buy", 1, 50000.0, "SIGNAL", "ORD3")  # 다른 유저

    recent = db.bot_trades_recent(UID, limit=10)
    assert [r["ticker"] for r in recent] == ["000660", "005930"]  # 최신순, UID 것만
    assert recent[0]["reason"] == "STOP_LOSS"


def test_bot_reset_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_position_upsert(UID, "005930", "삼성", 1, 100.0, 100.0, "2026-07-01")
    db.bot_trade_log(UID, "005930", "삼성", "buy", 1, 100.0, "SIGNAL", "O")
    db.kv_set(f"paper_account:{UID}", '{"cash": 5, "positions": {}}')
    db.bot_reset(UID)
    assert db.bot_positions_all(UID) == [] and db.bot_trades_recent(UID) == []
    assert db.kv_get(f"paper_account:{UID}") is None  # 시드로 리셋(kv 삭제)
