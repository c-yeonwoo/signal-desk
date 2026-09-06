"""실제 보유일 — 지평이 넷인데 실제 보유를 재는 코드가 없었다.

    accuracy.PRIMARY_HORIZON   20거래일   (실측 헤드라인)
    사전등록 look(정확도·IC)     5거래일
    bot.OUTCOME_HORIZON_DAYS    3거래일   (봇 판단 채점)
    실제 보유                    ?         ← 아무도 세지 않았다

"지평·진입/청산 관례·모집단·기간이 하나라도 다르면 리프트는 거짓이다"라고 적어 두고
정작 실제 보유일이 없었다.
"""

from __future__ import annotations

import pytest

from signal_desk import bot, db


DAY = 86400


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    db._CONN = None
    return tmp_path


def _buy(uid, tick, qty, ts):
    db.bot_trade_log(uid, tick, tick, "buy", qty, 100.0, "SIGNAL", "T", market="kr")
    _stamp(ts)


def _sell(uid, tick, qty, ts):
    db.bot_trade_log(uid, tick, tick, "sell", qty, 100.0, "TRAILING", "T", market="kr")
    _stamp(ts)


def _stamp(ts):
    c = db.conn()
    c.execute("UPDATE bot_trades SET ts=? WHERE id=(SELECT MAX(id) FROM bot_trades)", (ts,))
    c.commit()
    c.close()


def test_fifo_matches_lots(fresh):
    _buy(1, "A", 10, 1_000_000)
    _buy(1, "A", 10, 1_000_000 + 4 * DAY)
    _sell(1, "A", 20, 1_000_000 + 6 * DAY)
    st = bot.holding_period_stats(1)
    assert st["closed_lots"] == 2
    # 첫 로트 6일 · 둘째 2일, 수량 동일 → 평균 4.0
    assert st["mean_days"] == 4.0
    assert st["median_days"] == 4.0


def test_partial_sell_leaves_the_rest_open(fresh):
    _buy(1, "A", 10, 1_000_000)
    _sell(1, "A", 4, 1_000_000 + 3 * DAY)
    st = bot.holding_period_stats(1)
    assert st["closed_lots"] == 1
    assert st["median_days"] == 3.0


def test_weighted_mean_uses_quantity(fresh):
    _buy(1, "A", 1, 1_000_000)              # 1주 · 10일 보유
    _buy(1, "A", 99, 1_000_000 + 9 * DAY)   # 99주 · 1일 보유
    _sell(1, "A", 100, 1_000_000 + 10 * DAY)
    st = bot.holding_period_stats(1)
    assert st["mean_days"] == pytest.approx(1.09, abs=0.01), "수량 가중이 안 걸렸다"


def test_mismatch_is_stated_when_holding_is_shorter_than_every_horizon(fresh):
    _buy(1, "A", 10, 1_000_000)
    _sell(1, "A", 10, 1_000_000 + 1 * DAY)
    st = bot.holding_period_stats(1)
    assert st["mismatch"] is not None
    assert "가장 짧은 측정 지평" in st["mismatch"]
    assert "3거래일" in st["mismatch"]


def test_no_mismatch_when_holding_is_long_enough(fresh):
    _buy(1, "A", 10, 1_000_000)
    _sell(1, "A", 10, 1_000_000 + 30 * DAY)
    assert bot.holding_period_stats(1)["mismatch"] is None


def test_empty_says_why_not_zero(fresh):
    """0의 이유 — '아직 한 바퀴 안 돌았다'와 '고장'이 같아 보이면 안 된다."""
    st = bot.holding_period_stats(1)
    assert st["closed_lots"] == 0
    assert st["median_days"] is None
    assert "청산된 로트 없음" in st["reason"]


def test_unit_is_named(fresh):
    """달력일인지 거래일인지 이름에 적는다 — 환산 자체가 또 하나의 관례다."""
    _buy(1, "A", 10, 1_000_000)
    _sell(1, "A", 10, 1_000_000 + 5 * DAY)
    assert bot.holding_period_stats(1)["unit"] == "달력일"


def test_horizons_come_from_the_source_not_a_copy(fresh):
    st = bot.holding_period_stats(1)
    from signal_desk.signals import accuracy
    assert st["measured_horizons"]["실측 헤드라인"] == accuracy.PRIMARY_HORIZON
    assert st["measured_horizons"]["봇 판단 채점"] == bot.OUTCOME_HORIZON_DAYS


def test_reference_performance_carries_it():
    src = open(bot.__file__, encoding="utf-8").read()
    assert '"holding": holding_period_stats(' in src, \
        "장부에 안 실으면 측정 지평과 실제 보유가 한 화면에 안 보인다"
