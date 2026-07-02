from signal_desk import bot, db


def _creds():
    return {"app_key": "k", "app_secret": "s", "account_no": "1", "product_cd": "01", "env": "demo"}


def test_execute_reservation_fills_within_chase(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda now=None: True)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {"AAA": [100.0, 101.0]})  # 현재가 101
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: {"cash": 100_000.0, "total_eval": 100_000.0, "holdings": []})
    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda t, s, q, price=None, creds=None: orders.append((t, s, q)) or {"order_no": "1"})
    db.bot_reservation_add("AAA", "가", "buy", 100.0, 0.02, "테스트")  # 목표 100, +2%까지 → 상한 102

    out = bot.execute_reservations()
    assert out["ok"] is True
    assert out["executed"][0]["status"] == "filled"  # 101 <= 102 → 체결
    assert orders and orders[0][:2] == ("AAA", "buy")
    assert db.bot_reservations_pending() == []  # resolved


def test_execute_reservation_skips_when_price_ran_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda now=None: True)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {"AAA": [100.0, 110.0]})  # 현재가 110(+10%)
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: {"cash": 100_000.0, "total_eval": 100_000.0, "holdings": []})
    ordered = []
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: ordered.append(a) or {"order_no": "1"})
    db.bot_reservation_add("AAA", "가", "buy", 100.0, 0.02, "테스트")  # 상한 102

    out = bot.execute_reservations()
    assert out["executed"][0]["status"] == "skipped_price"  # 110 > 102 → 추격 안 함
    assert ordered == []  # 주문 없음


def test_execute_reservations_none_pending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda now=None: True)
    out = bot.execute_reservations()
    assert out["ok"] is True and out["executed"] == []
