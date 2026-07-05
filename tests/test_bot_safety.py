"""자동매매 안전장치 — kill switch·일일 손실한도·실계좌 가드."""

from signal_desk import bot, config


def test_kill_switch_blocks(monkeypatch):
    monkeypatch.setenv("BOT_KILL_SWITCH", "true")
    monkeypatch.setattr(config, "broker_backend", lambda: "paper")
    out = bot.run_once()
    assert out["ok"] is False and "긴급정지" in out["reason"]


def test_real_account_guard(monkeypatch):
    # KIS 실계좌(env!=demo)인데 ALLOW_REAL_ORDERS 미설정 → 주문 거부
    monkeypatch.setattr(config, "broker_backend", lambda: "kis")
    monkeypatch.setattr(config, "kis_credentials",
                        lambda: {"app_key": "k", "app_secret": "s", "account_no": "n",
                                 "product_cd": "01", "env": "prod"})
    monkeypatch.setattr(config, "allow_real_orders", lambda: False)
    monkeypatch.setattr(bot, "is_market_hours", lambda: True)
    out = bot.run_once()
    assert out["ok"] is False and "실계좌 주문 차단" in out["reason"]


def test_daily_loss_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_DAILY_LOSS_LIMIT_PCT", "0.08")
    # 최초: 기준선 기록만(미초과)
    assert bot._daily_loss_breached({"total_eval": 1_000_000.0}, dry_run=False) is False
    # 경미한 하락(-3%) → 미초과
    assert bot._daily_loss_breached({"total_eval": 970_000.0}, dry_run=False) is False
    # 한도 초과(-10%) → 신규매수 중단
    assert bot._daily_loss_breached({"total_eval": 900_000.0}, dry_run=False) is True
