"""Domestic diagnostic boundaries; future outcomes never choose the major list."""

import datetime as dt

import pytest

from scripts.measure.kr_selection_holding_audit import selection_census


def _fixture():
    dates = [(dt.date(2023, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(254)]
    days = {day: [{"ISU_CD": f"{i:05d}0", "ISU_NM": f"회사{i}", "MKTCAP": 1000 - i,
                   "TDD_OPNPRC": 0, "TDD_CLSPRC": 100 + index}
                  for i in range(21)] for index, day in enumerate(dates)}
    def row(day, ticker, kind, net):
        return {"date": day, "ticker": ticker, "kind": kind, "score": 2,
                "outcomes": {h: {"state": "matured", "net_pct": net} for h in ("1", "5", "20")}}
    pilot = {"source_level": "C_offline_price_only_current_engine", "registered_verdict": False,
             "signal_dates": dates[-2:], "rows": [row(dates[-2], "000000", "BUY", -5),
                 row(dates[-2], "000200", "HOLD", 20), row(dates[-1], "000000", "BUY", 1)]}
    return pilot, days


def test_census_keeps_missed_winner_and_freezes_major_names_before_outcomes():
    pilot, days = _fixture()
    result = selection_census(pilot, days)
    assert result["counts"]["buy_h5_negative"] == 1
    assert result["counts"]["h5_ge3_not_buy_label_days"] == 1
    assert result["counts"]["buy_h5_below_same_day_cohort"] == 1
    assert result["counts"]["unscored_candidate_slots"] == 397
    assert len(result["major_tickers_frozen_at_first_decision"]) == 20
    assert "000200" not in {r["ticker"] for r in result["major_tickers_frozen_at_first_decision"]}
    # Missing opens do not remove valid close inputs from the trend diagnostic.
    assert all(r["recent20_pct"] is not None for r in result["rows"])
    assert all(r["historical_full_engine"] is False for r in result["rows"])


def test_missing_outcome_is_not_turned_into_zero_return():
    pilot, days = _fixture()
    pilot["rows"][0]["outcomes"]["5"] = {"state": "price_gap"}
    result = selection_census(pilot, days)
    assert result["missing_outcomes"] == {"5:price_gap": 1}
    assert "net_pct" not in result["rows"][0]["horizons"]["5"]
    assert result["label_day_mean_net_pct"]["buy"] == 1


@pytest.mark.parametrize("change", ["protected", "registered", "duplicate", "outside"])
def test_census_rejects_scope_escape(change):
    pilot, days = _fixture()
    if change == "protected":
        pilot["signal_dates"] = ["2026-08-05"]
    elif change == "registered":
        pilot["registered_verdict"] = True
    elif change == "duplicate":
        pilot["rows"].append(pilot["rows"][0])
    else:
        pilot["rows"][0]["date"] = "2026-08-05"
    with pytest.raises(ValueError):
        selection_census(pilot, days)
