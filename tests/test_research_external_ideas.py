"""External opinions remain a versioned research input with honest timing."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from signal_desk import research_external_ideas as ideas


def _sample(**overrides):
    return {
        "source_url": "https://x.com/aleabitoreddit/status/123?ref=timeline",
        "author": "aleabitoreddit", "market": "US", "ticker": "AXTI",
        "instrument_kind": "options", "stance": "bullish", "horizon": "months",
        "thesis": "InP substrate bottleneck", "invalidation": "export permits do not arrive",
        "source_excerpt": "sample provided by owner", **overrides,
    }


def test_revisions_are_append_only_and_duplicate_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    first = ideas.add_manual(_sample())
    repeat = ideas.add_manual(_sample(source_url="https://x.com/aleabitoreddit/status/123"))
    assert first["added"] is True and repeat["added"] is False
    assert first["source_key"] == repeat["source_key"]
    changed = ideas.add_manual(_sample(thesis="Revised bottleneck thesis"))
    assert changed["revision"] == 2
    latest = ideas.list_latest()[0]
    assert latest["thesis"] == "Revised bottleneck thesis"
    assert latest["first_observed_at"] == ideas.revisions(first["source_key"])[0]["first_observed_at"]
    assert len(ideas.revisions(first["source_key"])) == 2
    assert latest["historical_reconstruction_eligible"] is False
    assert latest["live_order_eligible"] is False
    assert latest["registered_verdict"] is False
    assert latest["source_stance_verified"] is False
    assert latest["source_publication_verified"] is False
    assert latest["original_post_edit_verified"] is False


def test_one_post_can_hold_distinct_claims_without_double_counting_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    supply = ideas.add_manual(_sample(claim_key="supply", claim_kind="thesis"))
    exports = ideas.add_manual(_sample(claim_key="exports", claim_kind="thesis",
                                      thesis="Export license constraint"))
    assert supply["source_key"] != exports["source_key"]
    assert supply["post_key"] == exports["post_key"]
    assert len(ideas.list_latest()) == 2


def test_evidence_link_is_not_fact_verification_and_redaction_removes_copied_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    row = ideas.add_manual(_sample(evidence_url="https://investors.axt.com/news"))
    assert ideas.list_latest()[0]["evidence_status"] == "linked_unreviewed"
    assert ideas.redact(row["source_key"]) == 1
    assert ideas.list_latest() == []
    redacted = ideas.revisions(row["source_key"])[0]
    assert redacted["thesis"] == redacted["source_excerpt"] == ""
    assert redacted["redacted"] is True
    with pytest.raises(ValueError, match="redacted"):
        ideas.add_manual(_sample())


@pytest.mark.parametrize("change", [
    {"source_url": "http://x.com/a/status/1"},
    {"source_url": "javascript:alert(1)"},
    {"source_url": "https://u:p@x.com/a/status/1"},
    {"ticker": "NOT-KR", "market": "KR"},
    {"source_published_at": "2025-05-01"},
    {"source_published_at": "2999-01-01T00:00:00+00:00"},
    {"stance": "buy_now"},
])
def test_ambiguous_or_untrusted_input_is_rejected(tmp_path, monkeypatch, change):
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    with pytest.raises(ValueError):
        ideas.add_manual(_sample(**change))


def test_publication_time_does_not_backdate_first_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    before = dt.datetime.now(dt.timezone.utc)
    ideas.add_manual(_sample(source_published_at="2025-12-01T10:00:00-05:00"))
    row = ideas.list_latest()[0]
    assert row["source_published_at"] == "2025-12-01T15:00:00+00:00"
    assert dt.datetime.fromisoformat(row["forward_available_at"]) >= before
    assert row["historical_reconstruction_eligible"] is False


def test_admin_api_gate_and_handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "idea-admin@example.com")
    monkeypatch.setattr(ideas, "DB", tmp_path / "external_ideas.db")
    from signal_desk import api
    client = TestClient(api.app)
    assert client.get("/api/research/external-ideas").status_code == 401
    assert client.post("/api/research/external-ideas", json=_sample()).status_code == 401
    member = TestClient(api.app)
    assert member.post("/api/auth/signup", json={
        "email": "idea-member@example.com", "pw": "abcdef12"}).status_code == 200
    assert member.get("/api/research/external-ideas").status_code == 403
    assert member.post("/api/research/external-ideas", json=_sample()).status_code == 403
    assert client.post("/api/auth/signup", json={
        "email": "idea-admin@example.com", "pw": "abcdef12"}).status_code == 200
    saved = client.post("/api/research/external-ideas", json=_sample())
    assert saved.status_code == 200
    assert client.get("/api/research/external-ideas").json()["items"][0]["ticker"] == "AXTI"
    key = saved.json()["source_key"]
    assert len(client.get(f"/api/research/external-ideas/{key}/revisions").json()["items"]) == 1
    assert client.post(f"/api/research/external-ideas/{key}/redact").json()["redacted"] == 1
    page = client.get("/").text
    assert 'id="external-idea-research"' in page
    assert 'id="ei-body"' in page
