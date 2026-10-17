"""Selection accounting must not turn into hindsight or fabricated return attribution."""

import copy

import pytest

from scripts.measure.kr_financial_selection_attribution import decision_change, pack, summarize
from signal_desk.signals.engine import SignalResult


def _state(**kwargs):
    return {"kind": "HOLD", "score": 1.5, "rank": 7, "coverage": 1,
            "contributions": {"momentum": 1.5}, "gates": [], "relaxed_gates": [],
            "gate_blocked": False, "low_coverage": False, "valuation_percentile": None, **kwargs}


def test_rank_displacement_is_not_described_as_financial_score_improvement():
    before = _state()
    after = _state(kind="BUY", rank=6)
    result = decision_change(before, after, {}, {})
    assert result["role"] == "added_buy" and result["mechanism"] == "rank_only_displacement"
    assert all(v == 0 for v in result["contribution_delta"].values())
    assert decision_change(after, before, {}, {})["role"] == "removed_buy"


def test_peer_valuation_and_own_financial_changes_are_distinct():
    before, after = _state(valuation_percentile=50), _state(kind="BUY", valuation_percentile=40)
    assert decision_change(before, after, {"per": 10}, {"per": 10})["mechanism"] == "peer_valuation"
    own = decision_change(before, after, {}, {"per": 10})
    assert own["mechanism"] == "own_financial_inputs" and own["changed_metric_keys"] == ["per"]
    tied = _state(contributions={"fundamental": .75, "momentum": .75})
    assert decision_change(tied, tied, {}, {})["largest_positive_factors_after"] == ["fundamental", "momentum"]


def test_pack_uses_effective_contributions_not_positive_raw_indicators():
    signal = SignalResult(ticker="005930", name="삼성전자", score=1.5, kind="BUY", confidence=.6,
                          technical_score=3, fundamental_score=0, has_fundamental=False,
                          factor_scores={"technical": 1, "momentum": .5},
                          score_contributions={"technical": 0, "momentum": 1.5})
    assert pack(signal)["contributions"]["technical"] == 0
    signal.score = 2
    with pytest.raises(ValueError, match="account"):
        pack(signal)


def test_summary_is_date_equal_and_keeps_missing_prices_visible():
    rows = [{"date": day, "ticker": ticker, "h5": {"state": "matured", "net_pct": net},
             "same_day_cohort_h5_net_pct": 1}
            for day, ticker, net in (("2026-04-01", "005930", -6), ("2026-04-01", "000660", 2),
                                     ("2026-04-02", "005930", 4))]
    summary = summarize(rows)
    assert summary["date_equal_h5_net_pct"] == 1  # (-2 + 4)/2, not pooled zero
    assert summary["same_dates_cohort_h5_net_pct"] == 1 and summary["negative_rows"] == 1
    bad = copy.deepcopy(rows)
    bad[0]["h5"] = {"state": "price_gap"}
    assert summarize(bad)["date_equal_h5_net_pct"] is None
    assert summarize(bad)["missing_outcomes"] == {"price_gap": 1}
    assert summarize([])["date_equal_h5_net_pct"] is None
