"""Synthetic horizon, execution, and admission boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_selection_horizon_cross as lab


def market():
    spec = json.loads((Path(__file__).resolve().parents[1] / lab.f.PLAN_PATH).read_text())
    spec["periods"][0].update(decision_end="2024-10-15", tail_through="2024-12-31", arms=list(lab.ARMS))
    dates = lab.f.calendar("2024-09-30", "2024-12-31")
    rows = {d: {t: {"TDD_OPNPRC": 100., "TDD_CLSPRC": 100., "MKTCAP": c,
                    "ACC_TRDVOL": 10000000, "LIST_SHRS": 1000000}
                for t, c in (("A", 1), ("B", 2))} for d in dates}
    decisions = lab.f.calendar("2024-10-02", "2024-10-15")
    inputs = lab.f.Inputs({d: {"A": 0., "B": 0.} for d in decisions},
        {d: ["A"] for d in decisions}, rows, {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    return inputs, spec


@pytest.mark.parametrize("scenario", lab.SCENARIOS)
def test_actual_entry_anchors_horizon_and_t2(scenario):
    inputs, spec = market()
    result = lab.replay(inputs, spec, scenario, 0, lab.f.Budget())
    assert result["source_stop"] is None
    for arm, m in result["metrics"].items():
        assert m["complete"]
        assert m["net_pnl_krw"] == pytest.approx(-m["fees_krw"])
        assert not m["end_state"]["settlements"]
        buys = [v for v in m["fills"] if v["side"] == "buy"]
        sells = [v for v in m["fills"] if v["side"] == "sell"]
        assert buys and sells
        h = int(arm.rsplit("h", 1)[1])
        for buy, sell in zip(buys, sells):
            expected = lab.f.market_clock.next_sessions("kr", buy["on"], h+int(scenario.endswith("delay1")))[-1]
            assert sell["on"] == expected
            assert sell["shares"] == buy["shares"]
        assert all(v["ticker"] == ("A" if arm.startswith("legacy") else "B") for v in m["fills"])


def test_same_count_zero_candidates_and_future_prices_cannot_change_order():
    inputs, spec = market()
    first = min(inputs.forecasts)
    inputs.legacy[first] = []
    assert lab.selection(inputs, first)["cap"] == []
    original = lab.replay(inputs, spec, "75bp_full", 0, lab.f.Budget())
    assert not original["trace"][0]["orders"]
    inputs.legacy[first] = ["A"]
    original = lab.replay(inputs, spec, "75bp_full", 0, lab.f.Budget())
    changed = deepcopy(inputs)
    tomorrow = lab.f.market_clock.next_sessions("kr", first, 1)[0]
    for r in changed.rows[tomorrow].values():
        r["TDD_OPNPRC"] = 102.
    other = lab.replay(changed, spec, "75bp_full", 0, lab.f.Budget())
    assert original["trace"][0]["orders"] == other["trace"][0]["orders"]
    assert all(not any(v["on"] == tomorrow for v in m["fills"]) for m in other["metrics"].values())


def test_unit_gap_stops_all_arms_before_fill():
    inputs, spec = market()
    day = lab.f.market_clock.next_sessions("kr", min(inputs.forecasts), 1)[0]
    inputs.rows[day]["A"]["LIST_SHRS"] *= 2
    result = lab.replay(inputs, spec, "75bp_full", 0, lab.f.Budget())
    assert result["source_stop"]["field"] == "unreviewed_investment_unit_change"
    assert all(not m["complete"] and m["net_return_pct"] is None and not m["fills"] for m in result["metrics"].values())


def test_preserving_pending_exit_and_no_additional_held_buy():
    inputs, spec = market()
    result = lab.replay(inputs, spec, "75bp_full", 0, lab.f.Budget())
    m = result["metrics"]["legacy_h20"]
    assert sum(v["side"] == "buy" for v in m["fills"]) == 1
    book = lab.f.ExecutionBook(lab.f.execution_spec(spec, spec["periods"][0]), "75bp_full")
    book.pending = [{"ticker": "A", "sentinel": True}]
    book.holdings = {"A": 10}
    prior = deepcopy(book.pending)
    lab.submit_preserving(book, "2024-10-02", {"A": .1, "B": .1}, {"A": 100., "B": 100.},
                          {"A": 10000, "B": 10000}, 10000, {"A": 0, "B": 1}, "test")
    assert book.pending[:1] == prior
    assert book.pending[1]["ticker"] == "B"


def test_rejection_and_incomplete_are_distinct():
    groups = [{"scenario": sc, "phase": phase, "metrics": {
        a: {"complete": True, "net_return_pct": 2. if a == "legacy_h5" else 1., "price_mdd_pct": -1.}
        for a in lab.ARMS}} for sc in lab.SCENARIOS for phase in range(5)]
    assert lab.verdict(groups)["decision"] == "conditional_development_support"
    groups[0]["metrics"]["legacy_h5"]["net_return_pct"] = -20.
    assert lab.verdict(groups)["decision"] == "rejected"
    for g in groups:
        for m in g["metrics"].values():
            m.update(complete=False, net_return_pct=None, price_mdd_pct=None)
    assert lab.verdict(groups)["decision"] == "inconclusive_data"
