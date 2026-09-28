"""R16 first-observed flow cohorts must stay immutable and outside orders."""

import datetime as dt

import pandas as pd
from fastapi.testclient import TestClient

from signal_desk import api, db, market_clock, store
from signal_desk.signals import flow_shock_study as study


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    session = "2026-09-29"
    observed = "2026-09-29T07:00:00+00:00"
    flows = pd.DataFrame([
        {"ticker": "005930", "date": session, "foreign_net": -25, "inst_net": -5,
         "volume": 100, "content_hash": "shock", "observed_at": observed},
        {"ticker": "000660", "date": session, "foreign_net": 2, "inst_net": -2,
         "volume": 100, "content_hash": "control", "observed_at": observed},
    ])
    signals = pd.DataFrame([
        {"ticker": ticker, "market": "kr", "date": session,
         "exchange_session": session, "bar_asof": session,
         "session_valid": True, "observed_at": observed, "event_risk": 0,
         "decision_blocked": 0, "decision_severity": None}
        for ticker in ("005930", "000660")])
    monkeypatch.setattr(store, "load_flow_first_observations", lambda *a, **k: flows)
    monkeypatch.setattr(store, "load_signal_history", lambda market: signals)
    panel = {"005930": {"2026-09-28": 100.0, session: 95.0},
             "000660": {"2026-09-28": 100.0, session: 96.0}}

    def bundle(market):
        return ({t: [v for _, v in sorted(rows.items())] for t, rows in panel.items()},
                {t: sorted(rows) for t, rows in panel.items()})

    monkeypatch.setattr(store, "load_portfolio_close_bundle", bundle)
    return session, flows, signals, panel


def _after_close(day):
    return dt.datetime.fromisoformat(day + "T08:00:00+00:00")


def test_flow_first_observation_keeps_original_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.CACHE_DIR.mkdir(parents=True)
    pd.DataFrame([
        {"ticker": "005930", "date": "2026-09-29", "observed_at": "2026-09-29T07:00:00Z",
         "foreign_net": -30, "inst_net": 0, "volume": 100, "content_hash": "first"},
        {"ticker": "005930", "date": "2026-09-29", "observed_at": "2026-09-29T08:00:00Z",
         "foreign_net": 20, "inst_net": 0, "volume": 100, "content_hash": "revision"},
    ]).to_parquet(store.FLOW_OBSERVATIONS_FILE)
    out = store.load_flow_first_observations("2026-09-29", after=_after_close("2026-09-29") -
                                             dt.timedelta(hours=1, minutes=30),
                                             as_of=_after_close("2026-09-29") + dt.timedelta(hours=1))
    assert out.iloc[0]["content_hash"] == "first"


def test_flow_shock_captures_once_and_scores_first_observed_forward_prices(tmp_path, monkeypatch):
    session, flows, signals, panel = _seed(monkeypatch, tmp_path)
    now = _after_close(session)
    first = study.capture(now)
    assert first["saved"] == 1 and first["eligible"] == 2
    assert study.capture(now)["saved"] == 0
    snap = db.flow_shock_snapshots(1)[0]
    assert snap["candidate"]["flow_hash"] == "shock"
    assert snap["control"]["ticker"] == "000660"
    assert snap["live_eligible"] is False

    due = market_clock.next_sessions("kr", session, study.HORIZON)
    entry, exit_day = due[0], due[-1]
    panel["005930"][entry], panel["000660"][entry] = 95.0, 96.0
    assert study.collect(_after_close(entry))["marked"] == 1
    panel["005930"][exit_day], panel["000660"][exit_day] = 100.0, 97.0
    assert study.collect(_after_close(exit_day))["marked"] == 1
    result = study.evaluate(snap, db.flow_shock_marks(session, "005930"))
    assert result["status"] == "matured" and result["paired_delta_pp"] > 0
    report = study.report()
    assert report["counts"]["matured"] == 1 and report["live_eligible"] is False

    panel["005930"][session] = 94.0  # 원래 종가 수정은 과거 결과를 조용히 다시 계산하면 안 된다.
    assert study.collect(_after_close(exit_day))["halted"] == 1
    assert study.report()["counts"]["halted"] == 1


def test_flow_shock_never_calls_missing_veto_a_clean_bill_of_health(tmp_path, monkeypatch):
    session, flows, signals, panel = _seed(monkeypatch, tmp_path)
    signals.loc[signals["ticker"] == "005930", "event_risk"] = 1
    result = study.capture(_after_close(session))
    assert result["saved"] == 1 and result["unmatched_candidates"] == 1
    snap = db.flow_shock_snapshots(1)[0]
    assert snap["candidate"]["adverse_context"] == "veto_observed"
    assert snap["control"] is None
    assert study.evaluate(snap, {})["status"] == "unmatched"


def test_flow_shock_does_not_backdate_late_or_cross_market_observations(tmp_path, monkeypatch):
    session, flows, signals, panel = _seed(monkeypatch, tmp_path)
    flows.loc[flows["ticker"] == "005930", "observed_at"] = "2026-09-29T05:00:00+00:00"
    signals.loc[signals["ticker"] == "000660", "market"] = "us"
    result = study.capture(_after_close(session))
    assert result["saved"] == 0 and result["eligible"] == 0
    assert result["excluded"]["flow_invalid"] == 1
    assert result["excluded"]["signal_missing_or_stale"] == 1


def test_flow_shock_research_route_is_admin_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "flow-admin@example.com")
    monkeypatch.setattr(api.flow_shock_study, "report", lambda: {"live_eligible": False, "observed": 0})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/flow-shock").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/flow-shock").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "flow-admin@example.com", "pw": "abcdef12"})
    assert admin.get("/api/admin/research/flow-shock").json()["live_eligible"] is False
