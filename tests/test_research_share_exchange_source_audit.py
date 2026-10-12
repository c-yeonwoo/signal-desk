"""A merger filing is not proof of holder settlement or realized return."""

import json
from dataclasses import replace

import pytest

from scripts.measure import research_share_exchange_source_audit as source_audit


def _filing(terms: source_audit.Terms) -> bytes:
    y, m, d = source_audit._date_tokens(terms.record_date)
    ry, rm, rd = source_audit._date_tokens(terms.effective_on)
    ly, lm, ld = source_audit._date_tokens(terms.planned_listing_on)
    precision = "(소수점 이하 일곱째 자리까지만 표시함)" if terms.old_ticker == "042670" else ""
    return (f"<doc>{terms.old_issuer} {terms.successor_issuer} 기명식 보통주 "
            f"합병신주 배정 기준일 {y}년 {m}월 {d}일 "
            f"합병기일 {ry}년 {rm}월 {rd}일 "
            f"신주 상장 예정일 {ly}년 {lm}월 {ld}일 "
            f"발행 신주의 수 : {terms.issued_common_shares:,}주 "
            f"교부 비율 보통주 1주당 {terms.ratio}주 {precision} "
            "단주 주권상장일에 한국거래소에서 거래되는 종가를 기준으로 "
            "계산된 금액을 현금으로 지급할 예정 "
            "주식매수청구권 매수대금은 2025년 11월 24일 지급 완료</doc>").encode()


@pytest.mark.parametrize("terms", source_audit.TERMS)
def test_selected_terms_are_not_payment_evidence(terms):
    row = source_audit.parse_terms(_filing(terms), terms)
    assert row["reported_shares_per_old_share"] == terms.ratio
    assert row["fractional_cash_status"] == "planned_not_confirmed_paid"
    assert row["reported_ratio_precision"] == terms.ratio_precision


@pytest.mark.parametrize("change", [
    lambda b, t: b.replace(t.ratio.encode(), b"0.9999999"),
    lambda b, t: b.replace(t.successor_issuer.encode(), "무관한 회사".encode()),
    lambda b, t: b.replace("현금으로 지급할 예정".encode(), "지급 여부 불명".encode()),
    lambda b, t: b.replace("주권상장일에 한국거래소에서 거래되는 종가".encode(), "기준 가격 불명".encode()),
    lambda b, t: b.replace(f"{t.issued_common_shares:,}주".encode(), "1주".encode()),
])
def test_wrong_terms_fail_closed(change):
    terms = source_audit.TERMS[0]
    with pytest.raises(ValueError):
        source_audit.parse_terms(change(_filing(terms), terms), terms)


def test_infracore_ratio_precision_note_is_mandatory():
    terms = source_audit.TERMS[1]
    raw = _filing(terms).replace("(소수점 이하 일곱째 자리까지만 표시함)".encode(), b"")
    with pytest.raises(ValueError, match="precision"):
        source_audit.parse_terms(raw, terms)


def test_wrong_record_date_is_rejected():
    terms = source_audit.TERMS[0]
    wrong = replace(terms, record_date="2025-11-27")
    with pytest.raises(ValueError, match="record date"):
        source_audit.parse_terms(_filing(terms), wrong)


def test_real_frozen_sources_remain_partial():
    if not (source_audit.ROOT / "data/research" / "krx-daily-2025-09-12-v1.zip").exists():
        pytest.skip("local preserved research archives are not committed")
    report = source_audit.audit()
    stored = json.loads((source_audit.ROOT / "docs/research-share-exchange-source-audit-2026-10-12.json")
                        .read_text(encoding="utf-8"))
    assert report == stored
    assert len(report["cases"]) == 2
    assert {row["terms"]["old_ticker"] for row in report["cases"]} == {"010620", "042670"}
    for row in report["cases"]:
        assert row["terms"]["planned_new_listing_on"] == row["first_observed_successor_trade"]["session"]
        assert row["terms"]["fractional_cash_status"] == "planned_not_confirmed_paid"
        assert row["economic_return_pct"] is None
        assert row["feed_eligible"] is False
        assert row["account_share_delivery_verified"] is False
        assert row["fractional_cash_payment_verified"] is False
