"""텔레그램 페이퍼 체결은 한 봇의 확인된 체결만, 읽을 수 있는 가격·이유로 알린다."""

import datetime
from zoneinfo import ZoneInfo

from signal_desk import api, bot_alerts, config


def test_trade_style_defaults_to_balanced_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TRADE_STYLE", raising=False)
    assert config.telegram_trade_style() == "balanced"
    monkeypatch.setenv("TELEGRAM_TRADE_STYLE", "aggressive")
    assert config.telegram_trade_style() == "aggressive"
    monkeypatch.setenv("TELEGRAM_TRADE_STYLE", "ALL")
    assert config.telegram_trade_style() == "all"
    monkeypatch.setenv("TELEGRAM_TRADE_STYLE", "anything")
    assert config.telegram_trade_style() == "balanced"


def test_unselected_bots_and_failed_plans_never_queue(monkeypatch):
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "balanced")
    queued = []
    monkeypatch.setattr(api.notify, "enqueue", lambda *a, **k: queued.append((a, k)))
    monkeypatch.setattr(api.notify, "drain", lambda: None)
    result = {"ok": True, "buys": [{"name": "삼성전자", "ticker": "005930", "qty": 2,
                                      "fill_price": 70000, "order_no": "123", "reason": "SIGNAL", "ok": True}],
              "sells": [{"name": "실패", "ticker": "000001", "qty": 1,
                         "fill_price": 100, "order_no": "124", "ok": False}]}
    api._push_trades("kr", result, 900001)
    api._push_trades("kr", result, 900003)
    assert queued == []
    api._push_trades("kr", result, 900002)
    assert len(queued) == 1
    message, options = queued[0][0][0], queued[0][1]
    assert "균형형" in message and "매수 삼성전자(005930) 2주" in message
    assert "70,000원" in message and "140,000원" in message
    assert "실패" not in message and "실제 계좌 주문 아님" in message
    assert options["expires_at"] and options["dedupe_key"].startswith("paper-fill:900002:kr:")


def test_trade_message_reason_value_and_dedupe(monkeypatch):
    monkeypatch.setattr(config, "public_base_url", lambda: "https://app.example")
    rows = bot_alerts.trade_rows({"sells": [{"ok": True, "name": "삼성전자", "ticker": "005930",
                                             "qty": 1, "fill_price": 72000, "reason": "TRAILING",
                                             "order_no": "9"}], "buys": []})
    assert len(rows) == 1
    msg = bot_alerts.render("balanced", "kr", rows, total_eval=1000000, cash=200000,
                            now=datetime.datetime(2026, 9, 28, 9, 5, tzinfo=ZoneInfo("Asia/Seoul")))
    assert "09/28 09:05 KST" in msg
    assert "매도 삼성전자" in msg and "고점 대비 하락으로 이익 보호" in msg
    assert "봇 평가액 1,000,000원 · 현금 200,000원" in msg
    assert "https://app.example/#trading/live" in msg
    assert bot_alerts.dedupe_key(900002, "kr", rows) == bot_alerts.dedupe_key(900002, "kr", list(reversed(rows)))
    assert "현금 0원" in bot_alerts.render("balanced", "kr", rows, total_eval=100, cash=0, now=datetime.datetime(2026, 9, 28, tzinfo=ZoneInfo("Asia/Seoul")))


def test_reservation_message_requires_confirmed_fill_with_price(monkeypatch):
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "balanced")
    queued = []
    monkeypatch.setattr(api.notify, "enqueue", lambda *a, **k: queued.append((a, k)))
    monkeypatch.setattr(api.notify, "drain", lambda: None)
    result = {"ok": True, "executed": [
        {"status": "skipped_price", "ticker": "005930", "qty": 2},
        {"status": "filled", "ticker": "005930", "name": "삼성전자", "qty": 2,
         "order_no": "42", "fill_price": 70000},
    ]}
    api._push_reservations("kr", result, 900002)
    assert len(queued) == 1
    assert "예약 목표가 도달" in queued[0][0][0]
    assert "140,000원" in queued[0][0][0]
    api._push_reservations("kr", {"ok": True, "executed": [{"status": "filled", "qty": 2}]}, 900002)
    assert len(queued) == 1


def test_trade_alert_can_be_muted_or_explicitly_all(monkeypatch):
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "off")
    assert not bot_alerts.selected(900002, {900002: "balanced"})
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "all")
    assert bot_alerts.selected(900001, {900001: "conservative"})
    assert bot_alerts.selected(900003, {900003: "aggressive"})
