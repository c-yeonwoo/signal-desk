"""Maturity leakage, role isolation, fixed pools, and shared execution."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_entry_maturity as lab
from scripts.measure import kr_integer_risk_budget as parent
from scripts.measure import kr_frozen_forecast_policy as frozen


@pytest.fixture
def spec():
    root=Path(__file__).resolve().parents[1]
    s=json.loads((root/frozen.PLAN_PATH).read_text())
    s["portfolio"]["min_forecast_names"]=2
    s["periods"][0]["arms"]=json.loads((root/lab.PLAN_PATH).read_text())["arms"]
    return s


def archive():
    days=frozen.calendar("2024-08-01","2024-10-18")
    return {d:{str(i):{"forecast":.03-i*.001,"label":{"gross_return":.01,
        "exit_on":frozen.market_clock.next_sessions("kr",d,6)[-1],"missing":None}}
        for i in range(8)} for d in days}


def test_maturity_same_day_allowed_and_future_labels_never_read():
    a=archive(); day="2024-10-10"
    fitted=lab.matured_bias(day,a,.004)
    assert fitted["window_dates"]==20 and fitted["label_count"]==120
    assert fitted["latest_maturity"]==day and fitted["entry_bias"]==pytest.approx(.0175)
    for d,rows in a.items():
        if frozen.market_clock.next_sessions("kr",d,6)[-1]>day:
            for r in rows.values():
                r["label"]={}  # impossible to read future values, not merely alter their mean
    assert lab.matured_bias(day,a,.004)==fitted


def test_calibration_gaps_cannot_skip_a_date_and_bias_never_relaxes():
    a=archive(); d=lab.matured_bias("2024-10-10",a,.04)
    assert d["entry_bias"]==.04
    a[d["records"][-1]["forecast_on"]]["0"]["label"]["gross_return"]=None
    with pytest.raises(frozen.DataGap,match="label_unavailable"):
        lab.matured_bias("2024-10-10",a,.004)
    with pytest.raises(frozen.DataGap,match="insufficient"):
        lab.matured_bias("2024-08-10",a,.004)


def ctx(names):
    return {"session":"2024-10-10","stocks":{t:{"close":100.,"volume":100000.,"downside":.04} for t in names}}


def test_role_correction_preserves_raw_pool_and_held_off_pool_cannot_add(spec):
    f={str(i):.06-i*.005 for i in range(8)}; held={"7":1}
    q,choice=lab.choose(f,held,10000.,ctx(f),spec,{"entry_bias":.02,"held_bias":.004})
    assert choice["raw_top6"]==list("012345")
    assert "6" not in choice["coordinates"] and q["7"]<=1
    assert choice["coordinates"]["0"]["mu"]==pytest.approx(f["0"]-.02)
    assert choice["coordinates"]["7"]["mu"]==pytest.approx(f["7"]-.004)
    equal,_=lab.choose(f,held,10000.,ctx(f),spec,{"entry_bias":.004,"held_bias":.004})
    old,_=parent.choose_quantities({t:p-.004 for t,p in f.items()},held,10000.,ctx(f),spec)
    assert equal==old


def test_diagnostic_counts_unknown_not_losses_or_known_mean():
    rs=[{"ticker":"A","mu":.02,"gross_return":-.01},
        {"ticker":"B","mu":.02,"gross_return":None}]
    s=lab.row_stats(rs)
    assert s["unknown"]==1 and s["full_mean_error"] is None
    assert s["positive_negative_fraction_bounds"]==[.5,1.]
    assert s["known_only_mean_error"]==pytest.approx(.03)


@pytest.fixture
def market(spec):
    dates=frozen.calendar("2024-09-20","2024-11-01"); first=dates.index("2024-10-02"); days=dates[first:first+6]
    spec["periods"][0].update(decision_start=days[0],decision_end=days[-1],tail_through=dates[-1])
    spec["portfolio"]["initial_cash_krw"]=100000
    rows={d:{t:{"TDD_CLSPRC":100.,"TDD_OPNPRC":100.,"LIST_SHRS":1000000.,"ACC_TRDVOL":1000000.,"MKTCAP":2000-i}
        for i,t in enumerate("AB")} for d in dates}
    inputs=frozen.Inputs({d:{"A":.04,"B":.03} for d in days},{d:["A"] for d in days},rows,
        {d:dates[i-1] for i,d in enumerate(dates) if i},{},{})
    return spec,inputs


def replay(spec,inputs,bias):
    def factory(d,f):
        c=parent.decision_context(inputs,d,f)
        c["entry_calibration"]={"decision_on":d,"latest_maturity":d,"entry_bias":bias,"held_bias":.004}
        return c
    return frozen.replay_group(inputs,spec,"75bp_full",0,frozen.Budget(),target_selector=lab.selector(.004),
        forecast_required_arms=("candidate","prior_integer"),decision_context_factory=factory)


def test_shared_execution_prior_unchanged_and_correction_hash_bound(market):
    spec,inputs=market
    equal=replay(spec,inputs,.004)
    clean=parent.parent.parent_tools.without_ids
    assert all(m["complete"] for m in equal["metrics"].values())
    assert clean(equal["metrics"]["candidate"])==clean(equal["metrics"]["prior_integer"])
    changed=replay(spec,inputs,.1)
    assert clean(equal["metrics"]["prior_integer"])==clean(changed["metrics"]["prior_integer"])
    assert changed["metrics"]["candidate"]["buy_fills"]==0
    first=lambda g:next(r["decision"]["input_id"] for r in g["trace"] if r["decision"])
    assert first(equal)!=first(changed)


def test_selector_refuses_future_snapshot(spec):
    c=ctx("AB");c["entry_calibration"]={"decision_on":c["session"],"latest_maturity":"2024-10-11"}
    with pytest.raises(ValueError,match="future_calibration"):
        lab.selector(.004)("candidate",{"A":.04,"B":.03},{},[],{}, {},10000.,spec,context=c)


def test_cash_or_underused_comparison_cannot_pass(spec):
    gs=[{"scenario":s,"phase":p,"metrics":{a:{"complete":True,"net_return_pct":3. if a=="candidate" else 2.,"price_mdd_pct":-3.}
        for a in spec["periods"][0]["arms"]},"trace":[{"decision":{"terminal":False,"context":{"candidate_risk_budget":{"charge":.01}},
        "plans":{a:{"choice":{"planned_charge":.009}} for a in parent.MATCHED}}}]}
        for s in spec["execution"]["scenarios"] for p in range(5)]
    assert lab.verdict(gs,spec)["decision"]=="conditional_development_support"
    bad=deepcopy(gs)
    for g in bad:
        for a in parent.MATCHED:g["trace"][0]["decision"]["plans"][a]["choice"]["planned_charge"]=.001
    assert lab.verdict(bad,spec)["decision"]=="risk_match_insufficient"
    gs[0]["metrics"]["candidate"]["net_return_pct"]=0
    assert lab.verdict(gs,spec)["decision"]=="rejected"
