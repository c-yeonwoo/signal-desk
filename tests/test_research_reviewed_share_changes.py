"""A selected listing fact cannot silently certify a historical return path."""

import copy
import json
from pathlib import Path

import pytest

from scripts.measure import research_reviewed_share_changes as reviewed


def _source(preferred=False):
    extra = ("▶ 기명식 1우선주 8,159주 → 8,023주\n"
             "▶ 변경주식수 : -136주\n"
             "▶ 1우선주 표준코드 : KR7005931001 (단축코드:A005935)\n") if preferred else ""
    return ("<HTML><PRE>◎ 삼성전자(주) 변경상장(주식소각)\n"
            "▶ 기명식 보통주 5,919,637,922주 → 5,846,278,608주\n"
            "▶ 변경주식수 : -73,359,314주\n"
            f"{extra}"
            "④ 발행일(소각일) : 2026년04월02일\n"
            "⑦ 변경상장일 : 2026년04월14일\n"
            "⑧ 변경사유 : 주식소각\n"
            "▶ 보통주 표준코드 : KR7005930003 (단축코드:A005930)\n"
            "</PRE></HTML>").encode()


def _entry():
    return {"expected_text": "삼성전자", "url":
            "https://kind.krx.co.kr/external/2026/04/09/000375/20260409000536/68155.htm",
            "source_filing_date": "2026-04-09",
            "retrieved_at_utc": "2026-10-11T17:17:05+00:00"}


def _priority():
    return {"previous_session": "2026-04-13", "krx_change_session": "2026-04-14",
            "listed_shares_before": 5_919_637_922, "listed_shares_after": 5_846_278_608}


def test_common_share_cancellation_ignores_preferred_amount():
    row = reviewed.parse_listing(_source(preferred=True), _entry(), _priority(),
                                 ticker="005930")
    assert row.security_id == "KR7005930003"
    assert row.kind == "treasury_cancellation"
    assert row.effective_on == "2026-04-02"
    assert row.listing_session == "2026-04-14"
    assert row.listed_shares_before - row.listed_shares_after == 73_359_314
    assert row.holder_quantity_effect == "unchanged"
    assert row.coverage_state == "partial"


def test_source_identity_and_frozen_krx_mismatch_fail_closed():
    for raw, entry, frozen, ticker in (
        (_source(), _entry(), _priority(), "005935"),
        (_source().replace(b"KR7005930003", b"KR7005939003"), _entry(),
         _priority(), "005930"),
        (_source(), {**_entry(), "expected_text": "카카오"}, _priority(), "005930"),
        (_source(), _entry(), {**_priority(), "listed_shares_after": 123}, "005930"),
    ):
        with pytest.raises(ValueError, match="share fact|invalid reviewed"):
            reviewed.parse_listing(raw, entry, frozen, ticker=ticker)


def test_committed_review_keeps_all_original_episode_refs_and_no_return():
    path = Path(__file__).resolve().parents[1] / "docs/research-reviewed-share-count-changes-2026-10-12.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["schema"] == "research-reviewed-share-count-change-v1"
    assert len(report["facts"]) == report["distinct_issuer_sessions"] == 7
    assert report["overlap_episode_refs"] == 13
    assert report["left_censored_overlap_refs"] == 2
    assert report["coverage_state"] == "partial"
    assert report["economic_return_pct"] is None
    assert report["live_order_eligible"] is report["registered_verdict"] is False
    assert {row["kind"] for row in report["facts"]} == {
        "treasury_cancellation", "option_exercise_issuance"}
    assert all(row["source_availability"] == "retrospective_observation"
               and row["holder_quantity_effect"] == "unchanged"
               and row["coverage_state"] == "partial" for row in report["facts"])

    local_sources = path.parents[1] / "data/research"
    if all((local_sources / row[0]).is_file() for row in reviewed.SOURCES):
        assert reviewed.review(local_sources) == report
        replay = reviewed.replay_facts(local_sources)
        assert len(replay) == 7
        assert replay[("2025-11-14", "035720")].kind == "option_exercise_issuance"


def test_changed_frozen_queue_cannot_be_reclassified(tmp_path):
    corrupted = copy.deepcopy(json.loads(reviewed.PRIORITY.read_text(encoding="utf-8")))
    corrupted["events"][0]["listed_shares_before"] += 1
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen priority SHA"):
        reviewed.review(tmp_path, queue)
