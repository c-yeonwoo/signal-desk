"""Observed operational costs stay separate from investment verdicts."""

import datetime as dt

from signal_desk import db
from signal_desk.ingest import evidence_ops as ops


def test_first_daily_event_is_fixed_and_cost_is_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    original = {"status": "partial_failure", "requested": 4, "ok": 2,
                "no_data": 1, "failed": 1, "response_bytes": 1024, "raw_changed": 1}
    assert ops.record("dart", when=now, result=original) is True
    assert ops.record("dart", when=now, result={"status": "ok", "requested": 99}) is False
    assert ops.record("fed_g17", when=now, result={"status": "ok", "requested": 1,
                                                     "response_bytes": 2000,
                                                     "revised_periods": ["2026-08"]}) is True
    summary = ops.report(now=now + dt.timedelta(seconds=1))
    dart = summary["sources"]["dart"]
    assert dart["requested"] == 4 and dart["ok"] == 2 and dart["no_data"] == 1
    assert dart["failed_pct"] == 25.0 and dart["raw_changed"] == 1
    assert summary["sources"]["fed_g17"]["revised_periods"] == 1
    assert summary["monetary_cost_usd"] is None


def test_zero_requests_do_not_claim_zero_failure_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    assert ops.record("dart", when=now, result={"status": "missing_credentials", "requested": 0})
    report = ops.report(now=now + dt.timedelta(seconds=1))
    dart = report["sources"]["dart"]
    assert dart["observed_days"] == 1 and dart["requested"] == 0
    assert dart["failed_pct"] is None and dart["last_status"] == "missing_credentials"


def test_invalid_source_and_naive_time_rejected(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(ValueError):
        ops.record("live_orders", when=now, result={})
    with pytest.raises(ValueError):
        ops.record("dart", when=now.replace(tzinfo=None), result={})
