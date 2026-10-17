"""Publication/complete-bar clocks and next-bar cash execution invariants."""

import datetime as dt
from dataclasses import replace

import pytest

from signal_desk.signals.contract_event import Minute, ZONE, adoption, decide, decode_minutes, run_path


def _case():
    published = dt.datetime(2026, 6, 3, 10, tzinfo=ZONE)
    p = int(published.timestamp())
    event = {"receipt": "20260603000123", "ticker": "005930",
             "session": "2026-06-03", "published_at": published.isoformat()}
    bars = {}
    for label in range(p-600, p+6001, 60):
        close = 101.0 if label >= p+120 else 100.0
        volume = 200 if p+120 <= label <= p+360 else 100
        bars[label] = Minute(label, 100, 105, 95, close, volume)
    bars[p+4140] = replace(bars[p+4140], close=103)
    return p, event, bars


def test_future_minute_mutation_cannot_change_published_reaction_decision():
    p, event, bars = _case()
    result = decide(event, bars)
    assert result["state"] == "planned"
    assert result["decision_at"] == p+420
    assert result["entry_label"] == p+540
    assert result["reference_price"] == 101
    assert result["local_volume_ratio"] == 2
    changed = {label: replace(bar, close=200, volume=1_000_000) if label > p+360 else bar
               for label, bar in bars.items()}
    assert decide(event, changed) == result


def test_immediate_also_waits_for_publication_uncertainty_delivery_and_order_latency():
    p, event, bars = _case()
    proposal = decide(event, bars, immediate=True)
    assert proposal["decision_at"] == p+120
    assert proposal["entry_label"] == p+240
    assert proposal["reference_price"] == 100
    bars[p+360] = replace(bars[p+360], close=104)
    assert decide(event, bars)["state"] == "abstain"


def test_known_volume_fixes_integer_quantity_before_entry_and_exact_cost_cash():
    p, event, bars = _case()
    sample = [{"event": event, "stock": bars, "control": bars}]
    result = run_path(sample, "confirmed")
    trade = result["trades"][0]
    assert trade["shares"] == 10  # one percent of five known 200-share minutes
    assert trade["entry_label"] == p+540 and trade["exit_label"] == p+4140
    assert trade["net_pnl"] == pytest.approx(10 * (103*.99625 - 100*1.00375))
    bars[p+540] = replace(bars[p+540], open=90)
    cheaper = run_path(sample, "confirmed")["trades"][0]
    assert cheaper["shares"] == 10
    assert cheaper["net_pnl"] > trade["net_pnl"]


def test_entry_or_exit_gaps_are_not_replaced_by_later_prices_or_successful_events():
    p, event, bars = _case()
    sample = [{"event": event, "stock": bars, "control": bars}]
    del bars[p+4140]
    assert run_path(sample, "confirmed")["state"] == "incomplete_exit"
    assert run_path(sample, "confirmed")["net_return_pct"] is None
    del bars[p+540]
    assert run_path(sample, "confirmed")["state"] == "incomplete_entry"
    assert run_path(sample, "confirmed", fill_fraction=0)["net_return_pct"] == 0


def test_source_dates_and_duplicates_are_attested_and_protected_period_is_closed():
    body = {"rt_cd": "0", "output2": [{"stck_bsop_date": "20260603", "stck_cntg_hour": "100000",
             "stck_oprc": "100", "stck_hgpr": "102", "stck_lwpr": "99", "stck_prpr": "101", "cntg_vol": "10"}]}
    assert len(decode_minutes(body, "2026-06-03")) == 1
    with pytest.raises(ValueError, match="date_not_attested"):
        decode_minutes(body, "2026-06-02")
    body["output2"].append(dict(body["output2"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        decode_minutes(body, "2026-06-03")
    with pytest.raises(ValueError, match="invalid_minute_page"):
        decode_minutes(body, "2026-08-05")


def test_one_positive_trade_cannot_adopt_an_underpowered_event_policy():
    p, event, bars = _case()
    one = run_path([{"event": event, "stock": bars, "control": bars}], "confirmed")
    assert one["net_return_pct"] > 0
    scenarios = {key: {policy: one for policy in ("confirmed", "immediate", "market_control")}
                 for key in ("75bp_full_delay0", "120bp_full_delay0", "75bp_full_delay2")}
    verdict = adoption({"2026-06": scenarios, "2026-07": scenarios})
    assert verdict["adoption"] == "rejected" and verdict["live_order_eligible"] is False
    assert verdict["checks"]["at_least_12_source_days"] is False
    assert verdict["checks"]["at_least_8_trades_and_3_per_month"] is False


def test_missing_control_does_not_hide_a_complete_losing_strategy():
    _, event, bars = _case()
    losing = {**run_path([{"event": event, "stock": bars, "control": bars}], "confirmed"),
              "net_return_pct": -1}
    scenarios = {key: {policy: losing for policy in ("confirmed", "immediate", "market_control")}
                 for key in ("75bp_full_delay0", "120bp_full_delay0", "75bp_full_delay2")}
    scenarios["75bp_full_delay0"]["immediate"] = {"state": "incomplete_exit", "net_return_pct": None}
    verdict = adoption({"2026-06": scenarios, "2026-07": scenarios})
    assert verdict["adoption"] == "rejected"
    assert verdict["checks"]["all_required_paths_complete"] is False
    assert verdict["checks"]["both_months_net_positive_75bp"] is False
    assert verdict["checks"]["120bp_full_delay0_mean_positive"] is False
    assert verdict["checks"]["mean_excess_over_immediate"] is None
