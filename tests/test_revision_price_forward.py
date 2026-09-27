"""R13b forward observations are first-seen, costed and order-ineligible."""

import datetime as dt

from signal_desk import db
from signal_desk.signals import revision_price_forward as fw, revision_price_freeze as fr


def _snapshot():
    return {"version": fr.VERSION, "session": "2026-09-28", "market": "kr",
            "notional": 1_000_000.0, "top_k": 1,
            "cost_assumptions": fw.execution.cost_assumptions("kr"),
            "policies": {"eps_revision_only": ["A"], "revision_unreacted_price": ["B"]},
            "fixed_quantities": {"eps_revision_only": {"A": 9000},
                                 "revision_unreacted_price": {"B": 9000}},
            "selected": {"A": {"price": 100.0}, "B": {"price": 100.0}}}


def test_costed_next_close_and_missing_price_never_fall_back():
    s = _snapshot()
    days = fw.market_clock.next_sessions("kr", s["session"], fw.HORIZON)
    marks = {days[0]: {"A": 100.0, "B": 100.0},
             days[-1]: {"A": 100.0, "B": 120.0}}
    assert fw.evaluate(s, marks, completed_session=days[0])["status"] == "pending"
    assert "누락" in fw.evaluate(s, {}, completed_session=days[-1])["reason"]
    scored = fw.evaluate(s, marks, completed_session=days[-1])
    assert scored["ready"] and scored["delta_net_pp"] > 0
    assert scored["outcomes"]["eps_revision_only"]["net_return_pct"] < 0
    assert scored["source_available_at_verified"] is False and scored["live_eligible"] is False
    assert fw.evaluate(s, marks, completed_session=days[-1],
                       revision_halt="A:origin")["ready"] is False
    marks[days[0]]["B"] = 1_000.0
    assert "가격 갭" in fw.evaluate(s, marks, completed_session=days[-1])["reason"]


def test_first_mark_immutable_and_revisions_halt_episode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _snapshot()
    origin = s["session"]
    entry = fw.market_clock.next_sessions("kr", origin, fw.HORIZON)[0]
    db.revision_price_add_once(origin, s)
    prices = {"A": [100.0, 101.0], "B": [100.0, 102.0]}
    dates = {t: [origin, entry] for t in prices}
    monkeypatch.setattr(fw.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    now = dt.datetime.combine(dt.date.fromisoformat(entry), dt.time(9, 30), tzinfo=dt.timezone.utc)
    assert fw.collect(now)["marked"] == 2
    assert fw.collect(now)["marked"] == 0
    assert db.revision_price_marks(origin)[entry]["A"] == 101.0
    prices["A"][1] = 999.0
    assert fw.collect(now)["revision_halts"] == 1
    assert db.revision_price_halt(origin) == f"A:{entry}"
    assert db.revision_price_halt(origin, "B:entry") == f"A:{entry}"
