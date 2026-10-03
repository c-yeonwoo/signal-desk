"""Persisted DART bytes must agree with the public watchlist card arithmetic."""

import copy
import datetime as dt
import json
import sqlite3

import pytest
from fastapi import HTTPException

from signal_desk.ingest import financial_audit as audit
from signal_desk.ingest import financial_evidence as evidence
from signal_desk.signals import financial_change


NOW = "2026-10-03T12:00:00+00:00"


def _save(path, issuer, year, revenue, profit, *, raw_revenue=None, report="11012"):
    target = evidence.Target("dart", issuer, str(year), report)
    accession = f"{year}0814000001"
    rows = []
    for concept, statement, amount in (
        ("ifrs-full_Revenue", "IS", revenue if raw_revenue is None else raw_revenue),
        ("dart_OperatingIncomeLoss", "IS", profit),
    ):
        rows.append({"corp_code": issuer, "bsns_year": str(year), "reprt_code": report,
                     "fs_div": "CFS", "rcept_no": accession, "sj_div": statement,
                     "account_id": concept, "account_nm": concept,
                     "currency": "KRW", "thstrm_amount": str(amount)})
    evidence.archive(path, target, json.dumps({"status": "000", "list": rows}).encode(), observed_at=NOW)


def test_two_deterministic_issuers_match_archived_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_now", lambda: NOW)
    path = tmp_path / "e.db"
    for issuer in ("00126380", "00000001"):
        _save(path, issuer, 2025, 100, 10)
        _save(path, issuer, 2026, 120, 18)
    result = audit.sample_dart(path, as_of=NOW)
    assert result["status"] == "matched" and result["matched"] == 2
    assert [row["issuer"] for row in result["items"]] == ["00000001", "00126380"]
    check = result["items"][0]["checks"][0]
    assert check["status"] == "matched" and check["raw_previous"] == "100"
    assert check["raw_current"] == "120" and check["unit"] == "KRW"
    assert result["items"][0]["not_order_advice"]


def test_annual_report_card_also_matches_archived_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_now", lambda: NOW)
    path = tmp_path / "e.db"
    _save(path, "00126380", 2024, 100, 10, report="11011")
    _save(path, "00126380", 2025, 120, 18, report="11011")
    result = audit.audit_dart(path, issuer="00126380", as_of=NOW)
    assert result["status"] == "matched"
    assert {item["metric"] for item in result["checks"]} == {"revenue", "operating_income"}
    assert all(item["report"] == "11011" for item in result["checks"])


def test_card_number_mismatch_and_raw_tampering_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_now", lambda: NOW)
    path = tmp_path / "e.db"
    _save(path, "00126380", 2025, 100, 10)
    _save(path, "00126380", 2026, 120, 18)
    original = financial_change.describe_dart

    def altered(*args, **kwargs):
        card = copy.deepcopy(original(*args, **kwargs))
        card["metrics"]["revenue"]["current"] = "121"
        return card

    monkeypatch.setattr(financial_change, "describe_dart", altered)
    item = audit.audit_dart(path, issuer="00126380", as_of=NOW)
    assert item["status"] == "mismatch"
    assert any(check["metric"] == "revenue" and check["status"] == "mismatch"
               for check in item["checks"])
    monkeypatch.setattr(financial_change, "describe_dart", original)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE financial_observations SET raw=? WHERE source_url LIKE ?",
                     (b"{}", "%bsns_year=2026%"))
    assert audit.audit_dart(path, issuer="00126380", as_of=NOW)["status"] == "audit_error"


def test_not_ready_and_admin_only_no_network(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.setattr(evidence, "_now", lambda: NOW)
    path = tmp_path / "e.db"
    _save(path, "00126380", 2026, 120, 18)
    assert audit.audit_dart(path, issuer="00126380", as_of=NOW)["status"] == "not_ready"
    assert audit.sample_dart(path, as_of=NOW)["status"] == "insufficient_sample"
    monkeypatch.setattr(financial_change, "DEFAULT_ARCHIVE", path)
    monkeypatch.setattr(api, "_kst_now", lambda: dt.datetime.fromisoformat(NOW))
    monkeypatch.setattr(api, "_require_admin", lambda _request: False)
    with pytest.raises(HTTPException) as exc:
        api.dart_card_raw_audit_get(object())
    assert exc.value.status_code == 403
    monkeypatch.setattr(api, "_require_admin", lambda _request: True)
    assert api.dart_card_raw_audit_get(object())["status"] == "insufficient_sample"
    assert "/api/admin/evidence-audit/dart" in api._ADMIN_PATHS


def test_existing_nonarchive_file_is_not_treated_as_empty_sample(tmp_path):
    path = tmp_path / "invalid.db"
    path.write_bytes(b"not a database")
    assert audit.sample_dart(path, as_of=NOW)["status"] == "archive_error"
