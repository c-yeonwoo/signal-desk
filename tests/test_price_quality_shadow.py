"""Quality veto adds only prospective PIT metadata to the price control."""

import datetime as dt

import pandas as pd
from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import price_baseline_shadow as pb, price_quality_shadow as qs


def _base(session="2026-09-28"):
    return {"version": pb.VERSION, "market": "kr", "session": session,
            "observed_at": "2026-09-28T09:00:00+00:00", "notional": 1_000_000.0,
            "top_k": 2, "cost_assumptions": pb.execution.cost_assumptions("kr"),
            "policies": {"price_3factor": ["A", "B"], "sector_momentum": ["B"]},
            "selected": {"A": {"price": 100.0}, "B": {"price": 100.0}}}


def _pit(session="2026-09-28"):
    return pd.DataFrame([
        {"date": session, "ticker": "A", "bar_asof": session, "session_valid": True,
         "quality": 1, "quality_evaluable": 5},
        {"date": session, "ticker": "B", "bar_asof": session, "session_valid": True,
         "quality": 4, "quality_evaluable": 5}])


def test_quality_veto_never_backfills_and_us_uses_its_own_denominator():
    policies = qs.decide(["A", "B", "C"], {
        "A": {"points": 1, "evaluable": 5},
        "B": {"points": 1, "evaluable": 2},
        "C": {"points": 4, "evaluable": 5}})
    assert policies["quality_veto"] == ["B", "C"]
    assert policies["same_count_top_price"] == ["A", "B"]


def test_quality_capture_freezes_denominator_and_missing_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base, pit = _base(), _pit()
    db.price_baseline_add_once("kr", base["session"], base)
    monkeypatch.setattr(qs.store, "load_signal_history", lambda market: pit)
    now = dt.datetime(2026, 9, 28, 9, 30, tzinfo=dt.timezone.utc)
    assert qs.capture("kr", now)["saved"] == 1
    frozen = db.price_quality_get("kr", base["session"])
    assert frozen["eligible"] and frozen["quality"]["B"]["ratio"] == 0.8
    assert frozen["policies"]["quality_veto"] == ["B"]
    assert frozen["ratio_distribution"] == {"min": 0.2, "median": 0.5,
                                             "max": 0.8, "available": 2, "passed": 1}
    pit.loc[pit["ticker"] == "B", "quality"] = 0
    assert qs.capture("kr", now)["saved"] == 0
    assert db.price_quality_get("kr", base["session"]) == frozen

    # Another market/episode with a missing denominator is retained as blocked, not inferred as 5.
    day = "2026-10-28"
    us = {**base, "market": "us", "session": day, "observed_at": "2026-10-28T21:00:00+00:00"}
    db.price_baseline_add_once("us", day, us)
    us_pit = pd.DataFrame([{"date": day, "ticker": t, "bar_asof": day,
                            "session_valid": True, "quality": 1, "quality_evaluable": None}
                           for t in ("A", "B")])
    monkeypatch.setattr(qs.store, "load_signal_history", lambda market: us_pit if market == "us" else pit)
    db.kv_set("us_signal_snapshot_session", day)
    # 2026-10-29 08:00 UTC is within 20h after the Oct 28 NY close.
    result = qs.capture("us", dt.datetime(2026, 10, 29, 8, 0, tzinfo=dt.timezone.utc))
    assert result["saved"] == 1 and result["eligible"] is False
    assert db.price_quality_get("us", day)["invalid_tickers"] == ["A", "B"]


def test_quality_forward_uses_same_marks_and_cash_matched_control():
    base = _base()
    quality = {"version": qs.VERSION, "session": base["session"], "base_version": pb.VERSION,
               "base_observed_at": base["observed_at"], "eligible": True,
               "policies": {"price_3factor": ["A", "B"], "quality_veto": ["B"],
                            "same_count_top_price": ["A"]}}
    days = pb.market_clock.next_sessions("kr", base["session"], 20)
    marks = {days[0]: {"A": 100.0, "B": 100.0}, days[-1]: {"A": 80.0, "B": 120.0}}
    result = qs.evaluate(quality, base, marks, completed_session=days[-1])
    assert result["ready"] and result["delta_net_pp"] > 0
    assert result["selection_delta_pp"] > result["delta_net_pp"]
    assert result["entry_exposure_gap_pp"] <= qs.MAX_CASH_MATCH_GAP_PP
    assert result["outcomes"]["quality_veto"]["selected_slots"] == 1
    assert qs.evaluate(quality, base, {}, completed_session=days[-1])["ready"] is False
    assert qs.evaluate(quality, base, marks, completed_session=days[-1],
                       revision_halt="A:origin")["ready"] is False
    assert qs.evaluate(None, base, marks, completed_session=days[-1])["ready"] is False


def test_cash_matched_control_abstains_when_integer_share_exposure_differs():
    base = _base()
    base["selected"]["A"]["price"] = 400_000.0
    base["selected"]["B"]["price"] = 250_000.0
    quality = {"version": qs.VERSION, "session": base["session"], "base_version": pb.VERSION,
               "base_observed_at": base["observed_at"], "eligible": True,
               "policies": {"price_3factor": ["A", "B"], "quality_veto": ["B"],
                            "same_count_top_price": ["A"]}}
    days = pb.market_clock.next_sessions("kr", base["session"], 20)
    marks = {days[0]: {"A": 400_000.0, "B": 250_000.0},
             days[-1]: {"A": 400_000.0, "B": 250_000.0}}
    result = qs.evaluate(quality, base, marks, completed_session=days[-1])
    assert result["status"] == "blocked_exposure_match"
    assert result["entry_exposure_gap_pp"] > 1


def test_quality_api_requires_admin_and_hides_per_name_pit(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "quality-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    base = _base()
    db.price_baseline_add_once("kr", base["session"], base)
    db.price_quality_add_once("kr", base["session"], {"version": qs.VERSION,
        "session": base["session"], "base_version": pb.VERSION,
        "base_observed_at": base["observed_at"], "eligible": True,
        "quality": {"A": {"points": 1, "evaluable": 5}},
        "policies": {"price_3factor": ["A", "B"], "quality_veto": ["B"],
                     "same_count_top_price": ["A"]}})
    assert TestClient(api.app).get("/api/admin/research/price-quality").status_code == 401
    admin = TestClient(api.app)
    assert admin.post("/api/auth/signup", json={"email": "quality-admin@example.com", "pw": "abcdef12"}).status_code == 200
    response = admin.get("/api/admin/research/price-quality")
    assert response.status_code == 200
    assert response.json()["live_eligible"] is False
    assert "quality" not in response.json()["episodes"][0]["quality"]
    detailed = admin.get("/api/admin/research/price-quality?include_inputs=true")
    assert detailed.json()["episodes"][0]["quality"]["quality"]["A"]["points"] == 1
