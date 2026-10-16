"""Admin can distinguish new price checks from old paper fills."""

import time

from fastapi.testclient import TestClient

from signal_desk import api, db


def test_admin_paper_gate_report_counts_recent_reference_bots_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "gate-admin@example.com")
    client = TestClient(api.app)
    assert client.get("/api/admin/paper-order-gates").status_code == 401
    client.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert client.get("/api/admin/paper-order-gates").status_code == 403

    now = int(time.time())
    records = [
        ("old", 900002, "kr", "filled_buy", {}, now - 3600),
        ("checked", 900002, "kr", "filled_sell",
         {"paper_quote_check": {"status": "ready_receipt_only"}}, now - 100),
        ("hold", 900002, "kr", "deferred_source_time",
         {"paper_quote_check": {"source_time_state": "stale"}}, now - 50),
        ("us-checked", 900003, "us", "filled_buy",
         {"paper_quote_check": {"status": "ready_source_time_observed"}}, now - 20),
        ("stranger", 42, "kr", "filled_buy", {}, now - 10),
        ("expired", 900002, "kr", "deferred_stale_quote", {}, now - 49 * 3600),
    ]
    for key, uid, market, kind, payload, ts in records:
        db.execution_event_add(key, uid=uid, market=market, ticker="ABC",
                               event_type=kind, payload=payload, ts=ts)

    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "gate-admin@example.com", "pw": "abcdef12"})
    report = admin.get("/api/admin/paper-order-gates")
    assert report.status_code == 200
    data = report.json()
    assert data["sampled_events"] == 4 and data["truncated"] is False
    kr = data["markets"]["kr"]
    assert kr["legacy_fills"] == 1 and kr["checked_fills"] == 1
    assert kr["receipt_only_fills"] == 1
    assert kr["holds"]["deferred_source_time"] == 1
    assert kr["source_time_reasons"] == {"stale": 1}
    assert kr["last_event"]["type"] == "deferred_source_time"
    us = data["markets"]["us"]
    assert us["source_time_observed_fills"] == 1


def test_paper_gate_report_flags_truncated_sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(3):
        db.execution_event_add(f"event-{i}", uid=900001, market="kr", ticker="ABC",
                               event_type="deferred_market_closed", ts=100 + i)
    sample = db.paper_order_gate_events(0, (900001,), limit=2)
    assert sample["truncated"] is True
    assert [row["id"] for row in sample["events"]] == [3, 2]
