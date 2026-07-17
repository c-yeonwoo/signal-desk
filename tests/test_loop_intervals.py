"""시세 루프 / 봇·LLM 루프 간격 설정."""

from signal_desk import config


def test_default_intervals(monkeypatch):
    monkeypatch.delenv("BOT_RUN_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("QUOTE_REFRESH_INTERVAL_MINUTES", raising=False)
    assert config.bot_run_interval_minutes() == 30
    assert config.quote_refresh_interval_minutes() == 10


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BOT_RUN_INTERVAL_MINUTES", "60")
    monkeypatch.setenv("QUOTE_REFRESH_INTERVAL_MINUTES", "5")
    assert config.bot_run_interval_minutes() == 60
    assert config.quote_refresh_interval_minutes() == 5
