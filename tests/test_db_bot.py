from signal_desk import db


def test_bot_config_defaults_and_toggle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = db.bot_config_get()
    assert cfg == {"enabled": False, "max_positions": 10, "position_pct": 0.08, "updated": cfg["updated"],
                   "min_buy_score": 1.6, "max_new_buys_per_run": 2, "trading_style": "balanced"}

    db.bot_config_set_enabled(True)
    assert db.bot_config_get()["enabled"] is True
    db.bot_config_set_enabled(False)
    assert db.bot_config_get()["enabled"] is False


def test_bot_position_upsert_and_delete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.bot_positions_all() == []

    db.bot_position_upsert("005930", "삼성전자", 10, 70000.0, 72000.0, "2026-07-01")
    pos = db.bot_position_get("005930")
    assert pos == {"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 70000.0,
                   "peak_price": 72000.0, "entry_date": "2026-07-01",
                   "last_price": None, "last_pnl_pct": None}
    assert db.bot_positions_all() == [pos]

    # upsert 덮어쓰기
    db.bot_position_upsert("005930", "삼성전자", 15, 71000.0, 73000.0, "2026-07-01")
    assert db.bot_position_get("005930")["qty"] == 15

    db.bot_position_delete("005930")
    assert db.bot_position_get("005930") is None
    assert db.bot_positions_all() == []


def test_bot_trade_log_and_recent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_trade_log("005930", "삼성전자", "buy", 10, 70000.0, "SIGNAL", "ORD1")
    db.bot_trade_log("000660", "SK하이닉스", "sell", 5, 200000.0, "STOP_LOSS", "ORD2")

    recent = db.bot_trades_recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["ticker"] == "000660"  # 최신순
    assert recent[0]["reason"] == "STOP_LOSS"
    assert recent[1]["ticker"] == "005930"
