"""Synthetic account/calendar controls only, never empirical return evidence."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core
from signal_desk.signals import research_net_alpha_replay as replay
from signal_desk.signals.research_net_alpha_execution import ExecutionBook
from test_research_net_alpha_v2 import ISIN, OTHER, fact, reviewed


def world():
    cfg = json.loads(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_text())
    cfg.update(candidate_id="kr-separated-net-alpha-h5-v2", initial_cash_krw=100000,
        evaluation_end="2022-07-08", liquidation_through="2022-07-29",
        universe_count=2, min_feature_eligible=2,
        amendment={"labels":{"evaluation_outcome_fact_cutoff":"2026-10-18T00:00:00+09:00"}})
    days = replay.sessions(cfg["evaluation_start"], cfg["liquidation_through"])
    history = np.arange(253)
    price = np.exp(np.column_stack((.001*history+.01*np.sin(history),
        .0005*history+.005*np.cos(history))))*100
    features = core.features(price, np.ones((21, 2))*1000, [1e9, 2e9])
    snapshots = {d:{"date":d, "state":"ready", "tickers":["A", "B"], "features":features,
        "rows":[{"ticker":t,"state":"eligible"} for t in "AB"]} for d in days}
    bars = {d:{"opens":{t:{"price":100, "open_tradable":True, "source_id":"synthetic/"+d} for t in "AB"},
        "closes":{t:100 for t in "AB"}, "volumes":{t:100000 for t in "AB"},
        "caps":{"A":1e9, "B":2e9}} for d in days}
    model = {"candidate_id":cfg["candidate_id"], "selection_scale":[1]*11,
        "selection_coefficients":[.01]*11, "market_means":[0]*4, "market_scales":[1]*4,
        "market_coefficients":[.1, 0, 0, 0, 0]}
    kwargs = dict(snapshots=snapshots, bars=bars, identities={d:{"A":ISIN,"B":OTHER} for d in days},
        evidence=reviewed(), model=model, bias=0, input_ids={d:"synthetic/"+d for d in days})
    return cfg, kwargs


def test_fixed_five_phase_terminal_clock_does_not_scan_for_a_good_exit():
    cfg, _ = world()
    for phase in range(5):
        normal = replay.schedule(cfg, phase, "75bp_full")
        delayed = replay.schedule(cfg, phase, "75bp_delay1")
        last = normal["decisions"][-1]
        assert normal["terminal_open"] == market_clock.next_sessions("kr", last, 6)[-1]
        assert delayed["terminal_submit"] == normal["terminal_submit"]
        assert delayed["terminal_open"] == market_clock.next_sessions("kr", last, 7)[-1]
    with pytest.raises(ValueError, match="outside frozen"):
        replay.schedule(cfg, 5, "75bp_full")


def test_calendar_netting_and_forced_paths_reconcile_costs_and_keep_labels_out():
    cfg, kwargs = world()
    results = {a:replay.run_path(cfg, "75bp_full", a, 0, **kwargs) for a in ("cap", "cash", "forced_roundtrip")}
    for r in results.values():
        assert r["accounting_complete"]
        assert r["execution"]["holdings"] == {} and r["execution"]["pending"] == []
        assert r["net_return"] == pytest.approx(-r["execution"]["fees"]/100000)
        assert r["live_order_eligible"] is False
        assert r["adoption"] != "historical_research_candidate"
    assert results["cash"]["net_return"] == 0
    before = copy.deepcopy(results["forced_roundtrip"])
    for s in kwargs["snapshots"].values():
        s["labels"] = [999, -999]
    assert replay.run_path(cfg, "75bp_full", "forced_roundtrip", 0, **kwargs) == before


def test_missing_source_abstention_is_not_a_verified_zero_return_win():
    cfg, kwargs = world()
    kwargs["snapshots"][cfg["evaluation_start"]]["state"] = "input_gap"
    result = replay.run_path(cfg, "75bp_none", "candidate", 0, **kwargs)
    assert result["execution"]["fills"] == []
    assert not result["accounting_complete"] and result["net_return"] is None and result["mdd"] is None
    assert "scheduled_input_or_account_gap" in result["reasons"]
    cfg, kwargs = world()
    kwargs["evidence"] = type(kwargs["evidence"])()
    result = replay.run_path(cfg, "75bp_full", "cap", 0, **kwargs)
    # No falsely tradable fills in the absence of coverage.
    assert result["execution"]["fills"] == []
    assert not result["accounting_complete"] and result["net_return"] is None


def test_terminal_unpriced_residual_is_preserved_not_marked_as_zero():
    cfg, kwargs = world()
    terminal = replay.schedule(cfg, 0, "75bp_full")["terminal_open"]
    for day, bar in kwargs["bars"].items():
        if day >= terminal:
            bar["opens"] = {}; bar["closes"] = {}
    result = replay.run_path(cfg, "75bp_full", "cap", 0, **kwargs)
    assert result["execution"]["holdings"]
    assert result["net_return"] is None and result["mdd"] is None
    assert "terminal_assets_or_orders_unliquidated" in result["reasons"]


def test_independent_exit_needs_no_guessed_nav_and_does_not_reset_other_sell_retry():
    cfg, _ = world()
    b = ExecutionBook(cfg, "75bp_sell_half")
    b.holdings = {t:3 for t in "ABCDEFG"}  # Corporate delivery may exceed six assets.
    b.submit_exits("2022-07-01", ["A"], {"A":100}, {"A":10000}, input_id="first")
    b.open_session("2022-07-04", {})
    old = copy.deepcopy(b.pending)
    b.submit_exits("2022-07-04", ["B"], {"B":100}, {"B":10000}, input_id="exit")
    assert b.pending[0] == old[0] and b.pending[0]["attempts"] == 1
    assert all(o["side"] == "sell" for o in b.pending)
    saved = copy.deepcopy(b.state())
    assert b.submit_exits("2022-07-05", ["C"], {}, {}, input_id="unpriced") == []
    assert b.state() == saved


def test_late_reported_opening_halt_cannot_turn_noon_first_trade_into_open_fill():
    cfg, kwargs = world()
    late = fact("halt", "2022-07-04T09:00:00+09:00", published="2022-07-05",
                end_at="2022-07-04T12:00:00+09:00")
    kwargs["evidence"] = reviewed([late])
    original = json.dumps(kwargs["snapshots"], default=lambda a:a.tolist(), sort_keys=True)
    result = replay.run_path(cfg, "75bp_full", "cap", 0, **kwargs)
    assert not any(f["ticker"] == "A" and f["on"] == "2022-07-04" for f in result["execution"]["fills"])
    assert any(e["ticker"] == "A" and e["on"] == "2022-07-04" for e in result["execution"]["events"])
    assert json.dumps(kwargs["snapshots"], default=lambda a:a.tolist(), sort_keys=True) == original
