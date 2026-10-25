"""기존 거래 규칙 검사는 운영 매수 보류를 명시 해제한 상태에서 검증한다.

실제 서비스 기본값은 보류다. test_bot_buy_pause는 환경변수를 제거하거나 바꿔
미설정·잘못된 값의 보류, 명시 재개, 모든 주문 경계를 따로 검증한다.
"""

import pytest


@pytest.fixture(autouse=True)
def explicit_unpaused_bot_for_legacy_rule_tests(monkeypatch):
    monkeypatch.setenv("BOT_BUY_PAUSED", "false")
