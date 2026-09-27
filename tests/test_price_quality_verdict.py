"""Quality OOS inference counts blocked PIT inputs and requires cash-matched lift."""

import datetime as dt

from signal_desk.signals import price_baseline_shadow as pb, price_quality_verdict as gate


def _episodes(n=12, *, delta=1.0, selection=1.0):
    origin = dt.date(2026, 9, 28)
    return [{"version": pb.VERSION, "session": (origin + dt.timedelta(days=21 * i)).isoformat(),
             "quality_forward": {"ready": True, "status": "complete", "delta_net_pp": delta,
                                 "selection_delta_pp": selection}}
            for i in range(n)]


def _calendar(monkeypatch):
    monkeypatch.setattr(gate.market_clock, "next_sessions", lambda market, day, count: [
        (dt.date.fromisoformat(day) + dt.timedelta(days=i)).isoformat() for i in range(1, count + 1)])


def test_quality_requires_both_controls_and_never_promotes(monkeypatch):
    _calendar(monkeypatch)
    positive = gate.assess(_episodes(), market="kr", completed_session="2027-07-31")
    assert positive["status"] == "positive_research_signal"
    assert positive["auto_promote"] is False and positive["live_eligible"] is False
    assert gate.assess(_episodes(selection=0), market="kr",
                       completed_session="2027-07-31")["status"] == "cash_exposure_only"
    assert gate.assess(_episodes(delta=-1), market="us",
                       completed_session="2027-07-31")["status"] == "negative_research_signal"


def test_missing_quality_episode_stays_in_denominator(monkeypatch):
    _calendar(monkeypatch)
    rows = _episodes()
    rows[0]["quality_forward"] = {"ready": False, "status": "blocked"}
    rows.append({**rows[0], "session": "2026-10-01", "quality_forward": {"ready": True,
                 "status": "complete", "delta_net_pp": 100, "selection_delta_pp": 100}})
    result = gate.assess(rows, market="kr", completed_session="2027-07-31")
    assert result["status"] == "blocked_data_quality"
    assert result["matured_blocks"] == 12 and result["effective_blocks"] == 11
    assert result["blocked_sessions"] == ["2026-09-28"]
    assert gate.assess(rows, market="kr", completed_session="2027-02-01")["status"] == "awaiting_oos"
