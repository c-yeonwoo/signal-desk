"""The 17 source-linked listing classifications must not drift from KRX dates."""

import json

import pytest

from scripts.measure import research_action_listing_census as census


def test_fixed_census_matches_all_remaining_first_price_gaps():
    result = census.check()
    assert result["classified_remaining"] == 17
    assert result["classified_total_with_prior"] == 20
    assert result["by_event"] == {
        "demerger_relisting": 2, "kosdaq_to_kospi": 1,
        "new_listing": 13, "new_listing_infrastructure_company": 1}
    assert result["action_coverage"] == "partial"
    assert result["verified_total_return"] is False
    assert result["archived_originals"] == 15
    assert result["url_only"] == 2


def test_frozen_input_identity_cannot_be_substituted(tmp_path):
    payload = json.loads(census.CENSUS.read_text(encoding="utf-8"))
    payload["input_sha256"] = "0" * 64
    changed = tmp_path / "census.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen input identity"):
        census.check(census_path=changed)


def test_wrong_first_date_is_rejected(tmp_path):
    payload = json.loads(census.CENSUS.read_text(encoding="utf-8"))
    payload["records"][0]["first_close"] = "2025-03-15"
    changed = tmp_path / "census.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid source or KRX identity"):
        census.check(census_path=changed)


def test_original_archive_plan_cannot_silently_drop_a_source(tmp_path):
    plan = json.loads(census.KIND_PLAN.read_text(encoding="utf-8"))
    plan["documents"].pop()
    changed = tmp_path / "plan.json"
    changed.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="archive plan"):
        census.check(plan_path=changed)
