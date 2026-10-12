"""Selected exchange originals must not become a payment or a complete feed."""

import json
from pathlib import Path

import pytest

from scripts.measure import research_cash_exchange_source_audit as review


def _terms(amount="7,000", day="25"):
    return ("<html>쌍용씨앤이 한앤코시멘트홀딩스 현금교부형 주식교환 "
            "보통주식 주당 현금 " + amount + "원 교부할 예정 "
            "교환ㆍ이전일자 2024년 06월 " + day + "일</html>").encode()


def _effect(day="25", delisting="2024-07-09"):
    return ("<html>쌍용씨앤이주식회사 보통주 한앤코시멘트홀딩스 "
            "주식교환(교환일: 2024.06." + day + ")으로 "
            "완전자회사로 편입됨에 따라 상장폐지함 "
            "6.상장폐지일 " + delisting + "</html>").encode()


def test_selected_terms_and_effect_do_not_prove_payment():
    terms, effect = review.parse_terms(_terms()), review.parse_effect(_effect())
    assert terms["scheduled_gross_krw_per_share"] == 7000
    assert terms["amount_status"] == "scheduled"
    assert terms["payment_date"] is None
    assert effect["effective_on"] == "2024-06-25"
    assert effect["delisting_on"] == "2024-07-09"
    assert effect["cash_payment_confirmed"] is False


def test_changed_issuer_amount_effect_or_delisting_fails_closed():
    with pytest.raises(ValueError, match="issuer, class, payer"):
        review.parse_terms(_terms().replace("쌍용씨앤이".encode(), "다른회사".encode()))
    with pytest.raises(ValueError, match="amount differs"):
        review.parse_terms(_terms(amount="6,994"))
    with pytest.raises(ValueError, match="date"):
        review.parse_terms(_terms(day="32"))
    with pytest.raises(ValueError, match="listing identity"):
        review.parse_effect(_effect().replace("보통주".encode(), "우선주".encode()))
    with pytest.raises(ValueError, match="delisting date"):
        review.parse_effect(_effect(delisting="2024-06-25"))


def test_saved_review_is_partial_and_matches_local_originals_when_present(tmp_path):
    path = Path(__file__).resolve().parents[1] / (
        "docs/research-ssangyong-cash-exchange-audit-2026-10-12.json")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema"] == "research-cash-exchange-source-audit-v1"
    assert saved["terms"]["amount_status"] == "scheduled"
    assert saved["effect"]["effective_on"] == "2024-06-25"
    assert saved["payment_completion_verified"] is False
    assert saved["economic_return_pct"] is None
    assert saved["feed_eligible"] is saved["registered_verdict"] is False
    assert saved["action_coverage_state"] == "partial"
    if review.ARCHIVE.exists():
        assert review.audit() == saved
        altered = tmp_path / "changed-plan.json"
        altered.write_bytes(review.PLAN.read_bytes() + b" ")
        with pytest.raises(ValueError, match="frozen plan/archive SHA"):
            review.audit(plan_path=altered)
