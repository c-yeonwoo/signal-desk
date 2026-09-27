"""R15 US filing cohorts must not backdate relationship or price availability."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from signal_desk import db, market_clock
from signal_desk.signals import relation_event_study as study, relation_event_forward as forward, relation_event_verdict as verdict, relation_graph


def _event():
    return {"us_ticker": "NVDA", "event_type": "guidance_raise",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1234/5678/filing.htm",
            "evidence_quote": "The company increased its full-year revenue guidance after stronger demand.",
            "source_filed_date": "2026-09-25"}


def _relation():
    return {"us_customer": "NVDA", "kr_supplier": "000660", "revenue_exposure_pct": 12.5,
            "source_url": "https://www.sec.gov/Archives/edgar/data/1234/5677/relation.htm",
            "evidence_quote": "NVDA accounted for approximately 12.5 percent of our total revenue.",
            "source_published_date": "2026-09-01", "valid_from": "2026-09-01",
            "valid_until": "2027-09-01"}


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    reviewed = int(dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc).timestamp())
    edge = db.relation_edge_add(relation_graph.candidate(_relation()), observed_at=reviewed - 2000,
                                submitted_by=7)
    db.relation_edge_review(edge, verdict="approved", reviewed_at=reviewed - 1500,
                            reviewer_uid=7, note="SEC 원문에서 고객 및 매출 비율 확인")
    event = db.relation_event_add(study.candidate(_event()), observed_at=reviewed - 500,
                                  submitted_by=7)
    due = study.capture_session_for_review(reviewed)
    assert due == "2026-09-28"
    db.relation_event_review(event, verdict="approved", reviewed_at=reviewed,
                             reviewer_uid=7, note="SEC 원문 사건 방향 검토 완료", capture_session=due)
    return event, reviewed


def _panel(session="2026-09-28"):
    days = study._expected_days(session)
    tickers = ("000660", "005930", "042700", "000990")
    return ({ticker: [100.0] * len(days) for ticker in tickers},
            {ticker: days[:] for ticker in tickers})


def test_official_event_contract_rejects_news_and_missing_quote():
    proof = study.candidate(_event())
    assert proof["direction"] == 1 and proof["live_eligible"] is False
    assert study.candidate({**_event(), "source_url": "https://sec.gov/Archives/edgar/data/1234/5678/filing.htm"})["source_url"] == proof["source_url"]
    for change in ({"source_url": "https://news.example.com/nvda"},
                   {"source_url": "https://www.sec.gov/search-filings"},
                   {"source_url": _event()["source_url"] + "?duplicate=1"},
                   {"source_url": "https://www.sec.gov/Archives/edgar/data/1234/../5678/filing.htm"},
                   {"event_type": "earnings_beat"}, {"evidence_quote": "guidance up"},
                   {"source_filed_date": "20260925"}):
        with pytest.raises(ValueError):
            study.candidate({**_event(), **change})


def test_source_freshness_blocks_future_and_stale_relabeling():
    proof = study.candidate(_event())
    stamp = lambda day: int(dt.datetime.fromisoformat(day + "T12:00:00-04:00").timestamp())
    assert not study.source_fresh_at(proof, stamp("2026-09-24"))
    assert study.source_fresh_at(proof, stamp("2026-10-02"))
    assert not study.source_fresh_at(proof, stamp("2026-10-03"))


def test_same_edgar_filing_cannot_count_as_two_events(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    first = study.candidate(_event())
    db.relation_event_add(first, observed_at=100, submitted_by=7)
    second = study.candidate({**_event(), "source_url":
                              "https://sec.gov/Archives/edgar/data/0001234/5678/exhibit.htm"})
    assert first["source_filing_key"] == second["source_filing_key"]
    with pytest.raises(ValueError, match="이미 등록"):
        db.relation_event_add(second, observed_at=101, submitted_by=7)


def test_freeze_first_kr_close_only_and_no_late_relationship(tmp_path, monkeypatch):
    event_id, reviewed = _seed(tmp_path, monkeypatch)
    prices, dates = _panel()
    now = dt.datetime(2026, 9, 28, 9, tzinfo=dt.timezone.utc)
    frozen = study.freeze(db.relation_event_get(event_id), session="2026-09-28",
                          observed_at=now, prices=prices, dates=dates)
    assert frozen["ready"] and frozen["live_eligible"] is False
    assert frozen["groups"][0]["target"] == "000660"
    assert len(frozen["groups"][0]["controls"]) == 3
    assert frozen["quantities"]["linked"]["000660"] > 0
    assert abs(frozen["exposure_at_decision_pct"]["linked"] -
               frozen["exposure_at_decision_pct"]["sector_control"]) <= 1
    monkeypatch.setattr(study.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    assert study.capture(now)["saved"] == 1
    prices["000660"][-1] = 999.0
    assert study.capture(now)["saved"] == 0
    assert db.relation_event_snapshots()[0]["selected"]["000660"]["price"] == 100.0
    # A new relationship approved *after* event review may not enter its frozen cohort.
    new = db.relation_edge_add(relation_graph.candidate({**_relation(), "kr_supplier": "009150",
                                "source_url": "https://www.sec.gov/Archives/edgar/data/1234/5677/new.htm"}),
                               observed_at=reviewed + 10, submitted_by=7)
    db.relation_edge_review(new, verdict="approved", reviewed_at=reviewed + 11,
                            reviewer_uid=7, note="늦게 발견한 관계 근거")
    assert study.freeze(db.relation_event_get(event_id), session="2026-09-28",
                        observed_at=now, prices=prices, dates=dates)["groups"][0]["target"] == "000660"


def test_relation_approved_between_event_submission_and_review_is_too_late(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    observed = int(dt.datetime(2026, 9, 27, 11, tzinfo=dt.timezone.utc).timestamp())
    event = db.relation_event_add(study.candidate(_event()), observed_at=observed, submitted_by=7)
    edge = db.relation_edge_add(relation_graph.candidate(_relation()), observed_at=observed + 1,
                                submitted_by=7)
    db.relation_edge_review(edge, verdict="approved", reviewed_at=observed + 2,
                            reviewer_uid=7, note="사건 관측 이후 관계 확인")
    db.relation_event_review(event, verdict="approved", reviewed_at=observed + 3,
                             reviewer_uid=7, note="SEC 사건 원문 수동 대조", capture_session="2026-09-28")
    prices, dates = _panel()
    result = study.freeze(db.relation_event_get(event), session="2026-09-28",
                          observed_at=dt.datetime(2026, 9, 28, 9, tzinfo=dt.timezone.utc),
                          prices=prices, dates=dates)
    assert not result["ready"] and "관계 없음" in result["reason"]


def test_missing_first_session_is_permanent_halt(tmp_path, monkeypatch):
    event_id, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(study.store, "load_portfolio_close_bundle", lambda market: _panel("2026-09-29"))
    later = dt.datetime(2026, 9, 29, 9, tzinfo=dt.timezone.utc)
    result = study.capture(later)
    assert result["saved"] == 0 and result["halted"] == 1
    assert "소급" in db.relation_event_halt(event_id)
    assert db.relation_event_snapshots() == []


def _frozen(tmp_path, monkeypatch):
    eid, _ = _seed(tmp_path, monkeypatch)
    prices, dates = _panel()
    snapshot = study.freeze(db.relation_event_get(eid), session="2026-09-28",
                            observed_at=dt.datetime(2026, 9, 28, 9, tzinfo=dt.timezone.utc),
                            prices=prices, dates=dates)
    assert snapshot["ready"]
    assert db.relation_event_snapshot_add_once(eid, snapshot, int(dt.datetime.now(dt.timezone.utc).timestamp()))
    return eid, snapshot, prices, dates


def test_forward_first_observed_costed_and_directional(tmp_path, monkeypatch):
    eid, snapshot, prices, dates = _frozen(tmp_path, monkeypatch)
    entry, exit_day = market_clock.next_sessions("kr", snapshot["session"], study.HORIZON)[::19]
    for day, target in ((entry, 100.0), (exit_day, 110.0)):
        panel = {t: target if t == "000660" else 100.0 for t in snapshot["selected"]}
        db.relation_event_mark_once(eid, day, panel, 100)
    result = forward.evaluate(snapshot, db.relation_event_marks(eid), completed_session=exit_day)
    assert result["ready"] and result["directional_delta_net_pp"] > 0
    assert result["outcomes"]["linked"]["cost_drag_pct"] > 0
    assert forward.evaluate({**snapshot, "direction": -1}, db.relation_event_marks(eid),
                            completed_session=exit_day)["directional_delta_net_pp"] < 0
    assert forward.evaluate(snapshot, {}, completed_session=entry)["status"] == "pending"


def test_forward_collector_no_backfill_and_revision_halt(tmp_path, monkeypatch):
    eid, snapshot, prices, dates = _frozen(tmp_path, monkeypatch)
    entry = market_clock.next_sessions("kr", snapshot["session"], 1)[0]
    after_entry = market_clock.next_sessions("kr", entry, 1)[0]
    for t in prices:
        prices[t].append(110.0 if t == "000660" else 100.0)
        dates[t].append(entry)
    monkeypatch.setattr(forward.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    at = lambda day: market_clock._calendar("kr").schedule.loc[day]["close"].to_pydatetime() + dt.timedelta(hours=2)
    assert forward.collect(at(entry))["marked"] == 1
    assert forward.collect(at(entry))["marked"] == 0
    prices["000660"][-2] = 101.0
    assert forward.collect(at(after_entry))["halted"] == 1
    assert "수정/누락" in db.relation_event_halt(eid)
    assert forward.evaluate(snapshot, db.relation_event_marks(eid), completed_session=after_entry,
                            halt=db.relation_event_halt(eid))["status"] == "blocked"


def test_forward_missed_entry_permanently_blocks(tmp_path, monkeypatch):
    eid, snapshot, prices, dates = _frozen(tmp_path, monkeypatch)
    sessions = market_clock.next_sessions("kr", snapshot["session"], 2)
    for t in prices:
        prices[t].append(100.0)
        dates[t].append(sessions[-1])
    monkeypatch.setattr(forward.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    at = market_clock._calendar("kr").schedule.loc[sessions[-1]]["close"].to_pydatetime() + dt.timedelta(hours=2)
    assert forward.collect(at)["halted"] == 1
    assert sessions[0] in db.relation_event_halt(eid)
    assert db.relation_event_marks(eid) == {}


def test_forward_missed_exit_permanently_blocks(tmp_path, monkeypatch):
    eid, snapshot, prices, dates = _frozen(tmp_path, monkeypatch)
    sessions = market_clock.next_sessions("kr", snapshot["session"], study.HORIZON)
    entry, exit_day = sessions[0], sessions[-1]
    after_exit = market_clock.next_sessions("kr", exit_day, 1)[0]
    db.relation_event_mark_once(eid, entry, {t: 100.0 for t in snapshot["selected"]}, 100)
    monkeypatch.setattr(forward.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    at = market_clock._calendar("kr").schedule.loc[after_exit]["close"].to_pydatetime() + dt.timedelta(hours=2)
    assert forward.collect(at)["halted"] == 1
    assert exit_day in db.relation_event_halt(eid)


def test_approved_event_cannot_be_reclassified_after_outcome(tmp_path, monkeypatch):
    eid, reviewed = _seed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="최초 1회"):
        db.relation_event_review(eid, verdict="rejected", reviewed_at=reviewed + 30,
                                 reviewer_uid=7, note="사후 성과를 보고 제외", capture_session=None)
    assert len(db.relation_events_approved()) == 1


def test_fixed_looks_cluster_coverage_and_no_replacement(monkeypatch):
    days = [(dt.date(2026, 9, 29) + dt.timedelta(days=i)).isoformat() for i in range(280)]
    monkeypatch.setattr(verdict.market_clock, "next_sessions", lambda market, day, count:
                        days[days.index(day) + 1:days.index(day) + 1 + count])
    events = []
    for i in range(13):
        start = days[i * 21]
        events.append({"id": i + 1, "review": {"verdict": "approved", "capture_session": start},
                       "forward": {"ready": True, "status": "complete", "directional_delta_net_pp": 1.0}})
    completed = days[11 * 21 + 20]
    positive = verdict.assess(events, completed_session=completed)
    assert positive["look"] == 12 and positive["status"] == "positive_research_signal"
    assert positive["effective_clusters"] == 12
    events[0]["forward"] = {"ready": False, "status": "blocked"}
    assert verdict.assess(events, completed_session=days[-1])["status"] == "blocked_data_quality"
    events[0]["forward"] = {"ready": True, "status": "complete", "directional_delta_net_pp": -1.0}
    for event in events:
        event["forward"]["directional_delta_net_pp"] = -1.0
    assert verdict.assess(events, completed_session=completed)["status"] == "negative_research_signal"


def test_admin_event_requires_relation_and_manual_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "event-admin@example.com")
    from signal_desk import api

    monkeypatch.setattr(api, "_rl_hits", {})
    fixed = int(dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc).timestamp())
    monkeypatch.setattr(api.time, "time", lambda: fixed)
    headers = {"X-Signal-Desk-Relation": "review"}
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "event-admin@example.com", "pw": "abcdef12"})
    assert TestClient(api.app).get("/api/admin/research/relation-events").status_code in (401, 403)
    assert admin.post("/api/admin/research/relation-events", json=_event()).status_code == 403
    stale = admin.post("/api/admin/research/relation-events",
                       json={**_event(), "source_filed_date": "2026-09-01"}, headers=headers)
    assert stale.status_code == 400
    added = admin.post("/api/admin/research/relation-events", json=_event(), headers=headers)
    assert added.status_code == 200 and added.json()["status"] == "candidate"
    event_id = added.json()["id"]
    review_url = "/api/admin/research/relation-events/review"
    payload = {"event_id": event_id, "verdict": "approved", "note": "SEC 공시 원문 대조 완료"}
    assert admin.post(review_url, json=payload, headers=headers).status_code == 400
    assert admin.post(review_url, json={**payload, "source_checked": True}, headers=headers).status_code == 400
    now = fixed
    edge = db.relation_edge_add(relation_graph.candidate(_relation()), observed_at=now - 100,
                                submitted_by=7)
    db.relation_edge_review(edge, verdict="approved", reviewed_at=now - 50,
                            reviewer_uid=7, note="SEC 원문 매출 비율 대조 완료")
    monkeypatch.setattr(study, "capture_session_for_review", lambda at: "2026-09-28")
    approved = admin.post(review_url, json={**payload, "source_checked": True}, headers=headers)
    assert approved.status_code == 200 and approved.json()["capture_session"] == "2026-09-28"
    assert admin.get("/api/admin/research/relation-events").json()["events"][0]["review"]["verdict"] == "approved"
    assert admin.post(review_url, json={**payload, "verdict": "rejected"}, headers=headers).status_code == 400
