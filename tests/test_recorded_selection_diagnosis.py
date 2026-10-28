import copy
import datetime as dt

import pandas as pd
import pytest

from signal_desk import market_clock
from scripts.measure.recorded_selection_diagnosis import diagnose


def inputs():
    tickers = [f"S{i:02d}" for i in range(10)]
    first = dt.date(2026,6,1)
    dates = [(first+dt.timedelta(days=i)).isoformat() for i in range(61)
             if market_clock.is_session("kr",first+dt.timedelta(days=i))]
    raw = {d:[{"ISU_CD":t,"TDD_OPNPRC":"100","TDD_CLSPRC":"100"} for t in tickers]
           for d in dates}
    for r in raw["2026-07-20"]:
        if r["ISU_CD"] == "S00":
            r["TDD_CLSPRC"] = "120"
    signals = pd.DataFrame([{"date":"2026-07-16","ticker":t,
        "kind":"BUY" if t == "S00" else "HOLD"} for t in tickers])
    prices = pd.DataFrame([{"date":"2026-07-16","ticker":t,"close":100} for t in tickers])
    plan = {"protected_start":"2026-08-05","signal_from":"2026-07-16",
        "signal_until":"2026-08-04","price_until":"2026-07-31",
        "horizons":[1,5],"friction_roundtrip_pct":[0,.25,.75,1.2]}
    return signals,raw,prices,plan


def test_exact_recorded_cohort_expected_count_controls_and_cost_identity():
    s,raw,p,plan = inputs()
    out = diagnose(s,raw,p,plan)
    r = out["daily"][0]
    assert r["entry_date"] == "2026-07-20"  # July17 holiday is not an entry.
    assert r["selected_gross_pct"] == pytest.approx(20)
    assert r["uniform_expected_gross_pct"] == pytest.approx(2)
    assert r["risk_matched_expected_gross_pct"] == pytest.approx(0)
    assert r["selected_minus_uniform_pp"] == pytest.approx(18)
    assert r["risk_group_counts"] == [{"bucket":0,"selected_count":1,"nonselected_controls":1}]
    assert r["friction_scenarios"][2]["selected_net_pct"] == pytest.approx(19.25)
    assert not out["profitability_verified"] and not out["live_order_eligible"]
    assert out["summary"]["1"]["dates"] == 1


def test_gap_penalty_is_not_an_executable_close_entry_strategy():
    s,raw,p,plan = inputs()
    raw["2026-07-20"][0]["TDD_OPNPRC"] = "110"
    case = diagnose(s,raw,p,plan)["daily"][0]["selected_cases"][0]
    assert case["overnight_gap_pct"] == pytest.approx(10)
    assert case["gross_pct"] == pytest.approx((120/110-1)*100)
    assert case["close_reference_pct"] == pytest.approx(20)
    assert case["close_reference_minus_next_open_pp"] == pytest.approx(20-case["gross_pct"])


def test_missing_nonselected_future_price_never_improves_control_by_dropping_name():
    s,raw,p,plan = inputs()
    raw["2026-07-20"][1]["TDD_CLSPRC"] = "0"
    row = diagnose(s,raw,p,plan)["daily"][0]
    assert row["state"] == "incomplete_whole_cohort"
    assert row["missing_price_tickers"] == ["S01"]
    assert "selected_gross_pct" not in row


def test_unknown_risk_name_stays_in_primary_population():
    s,raw,p,plan = inputs()
    raw["2026-06-23"][1]["TDD_CLSPRC"] = "0"
    row = diagnose(s,raw,p,plan)["daily"][0]
    assert row["universe"] == 10 and row["state"] == "complete_cohort"
    assert row["missing_risk_tickers"] == ["S01"]
    assert row["risk_matched_expected_gross_pct"] is None
    assert row["uniform_expected_gross_pct"] == pytest.approx(2)


def test_protected_outcomes_and_source_extension_cannot_enter_summary():
    s,raw,p,plan = inputs()
    base = diagnose(s,raw,p,plan)
    s = pd.concat([s,s.assign(date="2026-08-05")],ignore_index=True)
    raw["2026-08-05"] = [{"ISU_CD":"S00","TDD_OPNPRC":"1","TDD_CLSPRC":"9999999"}]
    assert diagnose(s,raw,p,plan) == base
    s = s[s.date == "2026-07-16"].assign(date="2026-07-31")
    rows = diagnose(s,raw,p,plan)["daily"]
    assert all(r["state"] == "outside_frozen_price_window" for r in rows)


def test_duplicate_signal_or_price_version_rejected():
    s,raw,p,plan = inputs()
    with pytest.raises(ValueError,match="ambiguous recorded"):
        diagnose(pd.concat([s,s.iloc[:1]]),raw,p,plan)
    with pytest.raises(ValueError,match="ambiguous archived"):
        diagnose(s,raw,pd.concat([p,p.iloc[:1]]),plan)
    original = copy.deepcopy(raw)
    diagnose(s,raw,p,plan)
    assert raw == original
