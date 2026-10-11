"""Corporate-action price lineage remains separate from economic settlement."""

from dataclasses import replace

import pytest

from scripts.measure import research_action_price_lineage as lineage


def _row(ticker: str, close: int, volume: int) -> dict:
    return {"ISU_CD": ticker, "TDD_OPNPRC": str(close if volume else 0),
            "TDD_CLSPRC": str(close), "ACC_TRDVOL": str(volume),
            "ACC_TRDVAL": str(close * volume)}


def _days() -> dict[str, list[dict]]:
    return {
        "2025-12-01": [_row("010620", 223000, 100)],
        "2025-12-02": [_row("010620", 223000, 0)],
        "2025-12-03": [_row("010620", 223000, 0)],
        "2025-12-04": [_row("329180", 551000, 20)],
        "2025-12-05": [_row("329180", 524000, 20)],
        "2025-12-08": [_row("329180", 519000, 20)],
        "2025-12-09": [_row("329180", 504000, 20)],
        "2025-12-10": [_row("329180", 521000, 20)],
    }


def _case() -> lineage.Case:
    return replace(lineage.CASES[1], first_absent="2025-12-04")


def test_carry_forward_is_not_a_trade_or_verified_holder_return():
    result = lineage.analyze_price_lineage(_days(), _case())
    assert result["last_traded"]["session"] == "2025-12-01"
    assert result["zero_volume_carry"] == {
        "start": "2025-12-02", "end": "2025-12-03", "sessions": 2,
        "carried_close_krw": 223000}
    assert len(result["successor_first_five_trading_sessions"]) == 5
    assert result["settlement_verified"] is False
    assert result["total_return_verified"] is False
    assert result["strict_pit_eligible"] is False


@pytest.mark.parametrize("change", [
    lambda days: days["2025-12-02"][0].update({"ACC_TRDVOL": "1"}),
    lambda days: days["2025-12-03"][0].update({"TDD_CLSPRC": "224000"}),
    lambda days: days["2025-12-04"].append(_row("010620", 223000, 0)),
    lambda days: days["2025-12-04"][0].update({"ACC_TRDVOL": "0"}),
])
def test_inconsistent_carry_or_successor_path_fails_closed(change):
    days = _days()
    change(days)
    with pytest.raises(ValueError):
        lineage.analyze_price_lineage(days, _case())


def test_missing_session_or_insufficient_successor_path_fails_closed():
    days = _days()
    days.pop("2025-12-04")
    with pytest.raises(ValueError, match="first absent"):
        lineage.analyze_price_lineage(days, _case())
    days = _days()
    days.pop("2025-12-10")
    with pytest.raises(ValueError, match="fewer than five"):
        lineage.analyze_price_lineage(days, _case())
