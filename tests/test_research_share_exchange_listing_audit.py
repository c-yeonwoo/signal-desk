"""Selected later issuer evidence never becomes holder settlement evidence."""

import json

import pytest

from scripts.measure import research_share_exchange_listing_audit as audit_module


HEAVY = ("<html>HD현대중공업 HD현대미포 2025년 12월 1일 흡수합병 "
         "신주 발행 (유통주식수) 16,188,109</html>").encode()
CONSTRUCTION = ("<html>에이치디현대건설기계(주) 보통주 추가상장 "
                "기명식 보통주 30,616,505주 발행일 : 2026년01월02일 "
                "증자방법 : 합병 상장일 : 2026년01월26일 "
                "표준코드 : KR7267270007 (단축코드:A267270)</html>").encode()


def test_annual_issued_count_is_not_individual_credit_or_listing_session():
    row = audit_module.parse_heavy_annual(HEAVY)
    assert row["merger_issued_common_shares"] == 16188109
    assert row["aggregate_issuance_retrospectively_confirmed"] is True
    assert row["new_share_listing_session_confirmed"] is False
    assert row["individual_share_delivery_confirmed"] is False


def test_exchange_listing_notice_is_not_individual_credit():
    row = audit_module.parse_construction_notice(CONSTRUCTION)
    assert row["merger_issued_common_shares"] == 30616505
    assert row["notice_listing_on"] == "2026-01-26"
    assert row["individual_share_delivery_confirmed"] is False


@pytest.mark.parametrize("raw", [
    HEAVY.replace(b"16,188,109", b"16,188,108"),
    HEAVY.replace("HD현대미포".encode(), "무관한 회사".encode()),
    HEAVY.replace("2025년 12월 1일 흡수합병".encode(), "날짜 불명".encode()),
])
def test_annual_identity_and_count_are_required(raw):
    with pytest.raises(ValueError):
        audit_module.parse_heavy_annual(raw)


@pytest.mark.parametrize("raw", [
    CONSTRUCTION.replace(b"30,616,505", b"30,616,504"),
    CONSTRUCTION.replace("2026년01월26일".encode(), "2026년01월27일".encode()),
    CONSTRUCTION.replace(b"A267270", b"A267271"),
    CONSTRUCTION.replace("증자방법 : 합병".encode(), "증자방법 : 유상".encode()),
])
def test_listing_notice_identity_and_date_are_required(raw):
    with pytest.raises(ValueError):
        audit_module.parse_construction_notice(raw)


def test_frozen_listing_review_stays_partial():
    if not audit_module.ARCHIVE.exists():
        pytest.skip("local preserved KIND originals are not committed")
    report = audit_module.audit()
    stored = json.loads((audit_module.ROOT / "docs/research-share-exchange-listing-audit-2026-10-12.json")
                        .read_text(encoding="utf-8"))
    assert report == stored
    assert report["cases"]["042670"]["new_share_listing_notice_confirmed"] is True
    assert report["cases"]["010620"]["new_share_listing_session_confirmed"] is False
    assert report["economic_return_pct"] is None
    assert report["feed_eligible"] is False
