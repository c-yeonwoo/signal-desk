"""Observed operational costs stay separate from investment verdicts."""

import datetime as dt
import sqlite3

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


def test_sec_mixed_batch_keeps_actual_request_outcomes_and_first_status(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    first = {"status": "partial_failure", "requested": 2, "ok": 1, "failed": 1,
             "response_bytes": 3000, "raw_changed": 1}
    assert ops.record("sec", when=now, result=first)
    assert not ops.record("sec", when=now, result={"status": "ok", "requested": 9, "ok": 9})
    sec = ops.report(now=now + dt.timedelta(seconds=1))["sources"]["sec"]
    assert sec["requested"] == 2 and sec["ok"] == 1 and sec["failed"] == 1
    assert sec["failed_pct"] == 50.0 and sec["raw_changed"] == 1
    assert sec["last_status"] == "partial_failure"


def test_g17_marker_failure_keeps_actual_collection_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    assert ops.record("fed_g17", when=now, result={"status": "state_failure",
                                                   "collection_status": "ok", "requested": 1,
                                                   "response_bytes": 321})
    item = ops.report(now=now + dt.timedelta(seconds=1))["sources"]["fed_g17"]
    assert item["requested"] == item["ok"] == 1
    assert item["failed"] == 0 and item["response_bytes"] == 321
    assert item["last_status"] == "state_failure"


def test_invalid_source_and_naive_time_rejected(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(ValueError):
        ops.record("live_orders", when=now, result={})
    with pytest.raises(ValueError):
        ops.record("dart", when=now.replace(tzinfo=None), result={})


def test_archive_inventory_tracks_stable_anchors_without_creating_files(tmp_path):
    financial = tmp_path / "raw" / "financial.db"
    g17 = tmp_path / "raw" / "g17.db"
    empty = ops.archive_inventory(financial_path=financial, g17_path=g17)
    assert all(item["status"] == "not_recorded" for item in empty.values())
    assert not financial.exists() and not g17.exists()

    financial.parent.mkdir(parents=True)
    with sqlite3.connect(financial) as conn:
        conn.execute("CREATE TABLE financial_observations "
                     "(id TEXT, source_url TEXT, available_at TEXT)")
        conn.executemany("INSERT INTO financial_observations VALUES (?,?,?)", [
            ("dart-first", "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?corp_code=00126380",
             "2026-10-06T07:00:00+00:00"),
            ("sec-first", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
             "2026-10-07T07:00:00+00:00"),
        ])
    with sqlite3.connect(g17) as conn:
        conn.execute("CREATE TABLE fed_g17_observations (id TEXT, available_at TEXT)")
        conn.execute("INSERT INTO fed_g17_observations VALUES (?,?)",
                     ("g17-first", "2026-10-06T07:00:00+00:00"))
    first = ops.archive_inventory(financial_path=financial, g17_path=g17)
    assert {key: item["first_id"] for key, item in first.items()} == {
        "dart": "dart-first", "sec": "sec-first", "fed_g17": "g17-first"}
    assert all(item["observations"] == 1 for item in first.values())

    with sqlite3.connect(financial) as conn:
        conn.execute("INSERT INTO financial_observations VALUES (?,?,?)",
                     ("dart-later", "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?corp_code=00126380",
                      "2026-10-08T07:00:00+00:00"))
    later = ops.archive_inventory(financial_path=financial, g17_path=g17)
    assert later["dart"]["observations"] == 2
    assert later["dart"]["first_id"] == "dart-first"
    assert later["sec"]["first_id"] == "sec-first"


def test_archive_inventory_distinguishes_corruption_from_no_observations(tmp_path):
    invalid = tmp_path / "corrupt.db"
    invalid.write_bytes(b"not sqlite")
    result = ops.archive_inventory(financial_path=invalid, g17_path=tmp_path / "absent.db")
    assert result["dart"]["status"] == "archive_error"
    assert result["dart"]["observations"] is None
    assert result["sec"]["status"] == "archive_error"
    assert result["fed_g17"]["status"] == "not_recorded"


def test_storage_preflight_checks_all_archive_paths_without_claiming_persistence(tmp_path, monkeypatch):
    mount = str(tmp_path)
    app = tmp_path / "cache/app.db"
    financial = tmp_path / "raw/financial.db"
    g17 = tmp_path / "raw/g17.db"
    paths = {"expected_mount": mount, "app_db_path": app,
             "financial_path": financial, "g17_path": g17}
    monkeypatch.setattr(type(tmp_path), "is_mount", lambda self: self.resolve() == tmp_path.resolve())

    missing = ops.storage_preflight(mount_path="", **paths)
    assert missing == {"status": "mount_not_declared", "archive_paths_aligned": True,
                       "persistence_proven": False}
    observed = ops.storage_preflight(mount_path=mount, **paths)
    assert observed["status"] == "mount_observed"
    assert observed["persistence_proven"] is False
    outside = ops.storage_preflight(mount_path=mount, g17_path=tmp_path.parent / "elsewhere.db",
                                     **{key: value for key, value in paths.items() if key != "g17_path"})
    assert outside["status"] == "archive_path_outside_mount"
    assert outside["archive_paths_aligned"] is False


def test_storage_preflight_distinguishes_declared_directory_from_mount(tmp_path):
    result = ops.storage_preflight(
        mount_path=str(tmp_path), expected_mount=str(tmp_path),
        app_db_path=tmp_path / "cache/app.db", financial_path=tmp_path / "raw/financial.db",
        g17_path=tmp_path / "raw/g17.db")
    assert result["status"] == "mount_not_observed"
    assert result["persistence_proven"] is False


def test_storage_preflight_probe_error_does_not_break_admin_status(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    result = ops.storage_preflight(
        mount_path=str(tmp_path), expected_mount=str(tmp_path),
        app_db_path=tmp_path / "cache/app.db", financial_path=loop,
        g17_path=tmp_path / "raw/g17.db")
    assert result == {"status": "probe_error", "archive_paths_aligned": False,
                      "persistence_proven": False}
