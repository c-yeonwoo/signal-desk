"""낡은 실측이 사전등록 실행을 막는지.

중간 관측은 2026-09-23에 실효 8 · PIT 53으로 남았다. 보드가 그 8만 90%와 비교하면
PIT가 61이 되어도 자동 실행이 영영 안 돌고, 요건이 찬 날에도 잠그지 못한다.
매일 돌리는 것은 매일 백분위를 보는 것이므로, 늘어난 날짜의 상한이 90%에 닿을 때만 연다.
"""

from signal_desk import prereg


def _rq(**kw):
    base = {
        "min_effective_periods": 12,
        "min_pit_dates": 60,
        "effective_periods": 8,
        "pit_dates": 61,
        "effective_periods_source": "measured",
        "measured_pit_dates": 53,
        "hold": 5,
    }
    base.update(kw)
    return base


def test_eight_new_dates_cannot_close_a_four_period_gap():
    # 8 + (61-53)//5 = 9. 12의 90%는 10.8.
    assert prereg.run_due(_rq()) is False


def test_fifteen_new_dates_can_reach_the_band():
    # 8 + (68-53)//5 = 11.
    assert prereg.run_due(_rq(pit_dates=68)) is True


def test_an_estimate_does_not_invent_periods_from_a_missing_run():
    assert prereg.run_due(_rq(
        effective_periods_source="estimated", measured_pit_dates=None)) is False


def test_a_measurement_already_inside_the_band_stays_due():
    assert prereg.run_due(_rq(
        effective_periods=11, pit_dates=60, measured_pit_dates=60)) is True


def test_pit_short_of_its_own_band_blocks_the_run():
    assert prereg.run_due(_rq(
        effective_periods=11, pit_dates=50, measured_pit_dates=50)) is False


def test_maintenance_asks_run_due_and_the_board_keeps_the_measured_pit():
    from pathlib import Path
    root = Path(prereg.__file__).resolve().parent
    api = (root / "api.py").read_text(encoding="utf-8")
    board = (root / "store.py").read_text(encoding="utf-8")
    assert "near = prereg.run_due(rq)" in api
    assert 'prog["measured_pit_dates"]' in board and 'prog["hold"]' in board
