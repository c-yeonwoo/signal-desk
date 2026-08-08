"""시세 루프 / 봇·LLM 루프 간격 설정."""

from signal_desk import config


def test_default_intervals(monkeypatch):
    """빠른 틱 5분 · 느린 틱 30분. **둘을 나눈 이유가 비용이다.**

    빠른 틱에는 LLM이 없다(시세 + 손절·트레일링·예약 = 브로커 호출뿐). 느린 틱은 `advisor`
    (Opus)를 부르므로 30분을 유지한다 — 매수 후보는 일봉 종가 기반이라 자주 돌려도 거의
    그대로이고, 그러면 같은 판단에 돈만 더 낸다.
    """
    monkeypatch.delenv("BOT_RUN_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("QUOTE_REFRESH_INTERVAL_MINUTES", raising=False)
    assert config.bot_run_interval_minutes() == 30
    assert config.quote_refresh_interval_minutes() == 5
    assert config.quote_refresh_interval_minutes() < config.bot_run_interval_minutes(), \
        "빠른 틱이 느린 틱보다 잦아야 분리한 의미가 있다"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BOT_RUN_INTERVAL_MINUTES", "60")
    monkeypatch.setenv("QUOTE_REFRESH_INTERVAL_MINUTES", "5")
    assert config.bot_run_interval_minutes() == 60
    assert config.quote_refresh_interval_minutes() == 5
