"""R12: prospective price control cannot rewrite inputs or change live orders."""

import datetime as dt

import pandas as pd
from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import price_baseline_shadow as pb


def test_sector_shrink_uses_market_for_tiny_groups_and_positive_trend():
    rows = [{"ticker": f"A{i}", "baseline_score": 10 - i, "momentum": 0.1 + i / 100}
            for i in range(6)] + [
            {"ticker": "B", "baseline_score": 20, "momentum": -0.1}]
    sector = {r["ticker"]: ("large" if r["ticker"].startswith("A") else "tiny") for r in rows}
    result = pb.rank_candidates(rows, sector, 2)
    assert result["price_3factor"][0] == "B"
    assert "B" not in result["sector_momentum"]
    assert result["details"]["B"]["sector_shrink"] == 0
    assert 0 < result["details"][result["sector_momentum"][0]]["sector_shrink"] < 1


def _prices(session="2026-09-23"):
    prices = {f"T{i:02d}": [100.0] * 231 + [100.0 + i] * 22 for i in range(55)}
    calendar = pb.market_clock._calendar("kr")
    recent = [d.date().isoformat() for d in calendar.sessions_in_range("2025-01-01", session)][-253:]
    dates = {t: recent for t in prices}
    rows = pd.DataFrame([
        {"date": session, "ticker": t, "session_valid": True, "bar_asof": session,
         "momentum": round(ps[-22] / ps[0] - 1, 4)} for t, ps in prices.items()])
    return prices, dates, rows


def test_capture_freezes_pit_and_nonoverlap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, rows = _prices()
    monkeypatch.setattr(pb.store, "load_signal_history", lambda market: rows)
    monkeypatch.setattr(pb.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    monkeypatch.setattr(pb, "_sector_map", lambda market: {t: "all" for t in prices})
    now = dt.datetime(2026, 9, 23, 9, 30, tzinfo=dt.timezone.utc)
    first = pb.capture("kr", now)
    assert first["saved"] == 1, first
    frozen = db.price_baseline_recent("kr")[0]
    assert frozen["eligible"] == 55 and frozen["top_k"] == 2
    assert frozen["live_eligible"] is False
    assert frozen["policies"]["sector_momentum"]
    rows.loc[0, "momentum"] = -1.0
    assert pb.capture("kr", now)["saved"] == 0
    assert db.price_baseline_recent("kr")[0] == frozen
    assert pb.capture("kr", dt.datetime(2026, 9, 24, 9, 30, tzinfo=dt.timezone.utc))["saved"] == 0


def test_capture_abstains_if_pit_price_generation_disagrees(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, rows = _prices()
    rows.loc[:5, "momentum"] = 2.0
    monkeypatch.setattr(pb.store, "load_signal_history", lambda market: rows)
    monkeypatch.setattr(pb.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    result = pb.capture("kr", dt.datetime(2026, 9, 23, 9, 30, tzinfo=dt.timezone.utc))
    assert result["saved"] == 0 and result["coverage"] < 0.95


def test_us_requires_independent_pit_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # US market finished on Sep 23; KR date is already Sep 24. Never reuse KR PIT.
    now = dt.datetime(2026, 9, 24, 8, 0, tzinfo=dt.timezone.utc)
    result = pb.capture("us", now)
    assert result["saved"] == 0
    assert "PIT" in result["reason"]


def test_forward_exit_cost_missing_price_and_revision_halt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    origin = "2026-09-23"
    sessions = pb.market_clock.next_sessions("kr", origin, 20)
    snapshot = {"version": pb.VERSION, "market": "kr", "session": origin,
                "notional": 1_000_000.0, "top_k": 1,
                "cost_assumptions": pb.execution.cost_assumptions("kr"),
                "policies": {"price_3factor": ["A"], "sector_momentum": ["B"]},
                "selected": {"A": {"price": 100.0}, "B": {"price": 100.0}}}
    marks = {sessions[0]: {"A": 100.0, "B": 100.0},
             sessions[-1]: {"A": 100.0, "B": 120.0}}
    assert pb.evaluate(snapshot, marks, completed_session=sessions[0])["status"] == "pending"
    assert "누락" in pb.evaluate(snapshot, {}, completed_session=sessions[-1])["reason"]
    result = pb.evaluate(snapshot, marks, completed_session=sessions[-1])
    assert result["delta_net_pp"] > 0
    assert result["outcomes"]["price_3factor"]["net_return_pct"] < 0  # round trip costs
    assert pb.evaluate(snapshot, marks, completed_session=sessions[-1],
                       revision_halt="A:2026-09-23")["ready"] is False
    assert db.price_baseline_add_once("kr", origin, snapshot)
    assert not db.price_baseline_add_once("kr", origin, {"tampered": True})
    assert db.price_baseline_mark_once("kr", origin, sessions[0], marks[sessions[0]], 1) == 2
    assert db.price_baseline_mark_once("kr", origin, sessions[0], {"A": 500}, 2) == 0
    assert db.price_baseline_marks("kr", origin)[sessions[0]]["A"] == 100
    assert db.price_baseline_halt("kr", origin, "A:origin") == "A:origin"
    assert db.price_baseline_halt("kr", origin, "B:origin") == "A:origin"


def test_collect_forward_freezes_first_price_and_detects_revision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    origin = "2026-09-23"
    entry = pb.market_clock.next_sessions("kr", origin, 20)[0]
    snapshot = {"version": pb.VERSION, "market": "kr", "session": origin,
                "selected": {"A": {"price": 100.0}}, "policies": {"price_3factor": ["A"],
                "sector_momentum": ["A"]}}
    assert db.price_baseline_add_once("kr", origin, snapshot)
    prices = {"A": [100.0, 101.0]}
    dates = {"A": [origin, entry]}
    monkeypatch.setattr(pb.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    now = dt.datetime.combine(dt.date.fromisoformat(entry), dt.time(9, 30), tzinfo=dt.timezone.utc)
    assert pb.collect_forward("kr", now)["marked"] == 1
    prices["A"][1] = 200.0
    assert pb.collect_forward("kr", now)["revision_halts"] == 1
    assert db.price_baseline_marks("kr", origin)[entry]["A"] == 101.0
    assert db.price_baseline_halt("kr", origin) == f"A:{entry}"


def test_read_api_is_admin_only_and_hides_inputs(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "price-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    db.price_baseline_add_once("kr", "2026-09-23", {"version": pb.VERSION,
        "market": "kr", "session": "2026-09-23", "selected": {"SECRET": {"price": 100}},
        "config": {"private": 1}, "policies": {"price_3factor": [], "sector_momentum": []},
        "notional": 1000, "top_k": 1, "cost_assumptions": pb.execution.cost_assumptions("kr")})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/price-baseline").status_code == 401
    admin = TestClient(api.app)
    assert admin.post("/api/auth/signup", json={"email": "price-admin@example.com", "pw": "abcdef12"}).status_code == 200
    result = admin.get("/api/admin/research/price-baseline")
    assert result.status_code == 200
    assert result.json()["live_eligible"] is False
    assert "selected" not in result.json()["episodes"][0]
    assert "config" not in result.json()["episodes"][0]
    assert "SECRET" in admin.get("/api/admin/research/price-baseline?include_inputs=true").json()["episodes"][0]["selected"]
