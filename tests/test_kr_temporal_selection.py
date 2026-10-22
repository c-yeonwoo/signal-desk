from copy import deepcopy

import pytest

from scripts.measure import kr_temporal_selection as lab


def test_purge_and_fold_preprocessing_ignore_future_values():
    cfg = {"train_start":"2024-04-01", "features":["x"], "standardized_clip":8,
        "alpha_weighted_mean_objective":.1}
    rows = [{"date":"2024-04-01", "ticker":str(i), "features":[float(i)],
        "state":"matured", "target":i*.01, "exit_date":"2024-04-09"} for i in range(3)]
    future = {"date":"2024-05-30", "ticker":"future", "features":[999.],
        "state":"matured", "target":999., "exit_date":"2024-06-03"}
    model = lab.fit_fold(rows+[future], cfg, "2024-06-03")
    assert model == lab.fit_fold(rows, cfg, "2024-06-03")
    assert model["training_rows"] == 3 and model["last_label_exit"] < "2024-06-03"
    assert model["means"] == [1.]


def test_missing_label_cannot_remove_top_candidate():
    rs = [{"ticker":str(i), "state":"matured", "target":.02} for i in range(160)]
    rs[0] = {"ticker":"0", "state":"share_history_unverified"}
    c = lab.cohort(rs, {r["ticker"]:160-i for i,r in enumerate(rs)})
    assert "0" in c["selected"] and c["gross"] is None and c["universe_mean"] is None
    assert c["universe_missing"] == 1


def records():
    return [{"date":str(i), "common_missing":False,
        "ridge14":{"targets":[.02]*6, "gross":.02, "universe_mean":0.},
        "momentum12_skip21":{"targets":[.01]*6, "gross":.01, "universe_mean":0.}} for i in range(10)]


def fold(rs):
    return {"start":"test", "records":rs, "paths":[lab.path(rs,a,c,p)
        for a in ("ridge14","momentum12_skip21") for c in (75,120) for p in range(5)]}


def test_cost_compounding_and_same_missing_dates_for_both_arms():
    rs = records(); result = lab.path(rs,"ridge14",75,0)
    assert result["net_cohort_return_pct"] == pytest.approx(((1.02*(1-.00375)/(1+.00375))**2-1)*100)
    rs[0]["common_missing"] = True
    for arm in ("ridge14","momentum12_skip21"):
        result = lab.path(rs,arm,75,0)
        assert not result["complete"] and result["net_cohort_return_pct"] is None
        assert result["missing_dates"] == ["0"]


def test_admission_rejection_and_unknown_are_distinct():
    rs = records()
    assert lab.verdict([fold(rs)])["decision"] == "research_selection_support"
    partial = records(); partial[0]["common_missing"] = True
    partial[1]["ridge14"]["targets"] = [-.2]*6
    assert lab.verdict([fold(partial)])["decision"] == "rejected"
    bad = deepcopy(rs)
    for r in bad:r["ridge14"]["targets"] = [-.01]*6
    assert lab.verdict([fold(bad)])["decision"] == "rejected"
    rs[0]["common_missing"] = True
    assert lab.verdict([fold(rs)])["decision"] == "not_evaluable"
    rs = records(); rs[0]["ridge14"]["universe_mean"] = None
    assert lab.verdict([fold(rs)])["decision"] == "research_selection_support"
