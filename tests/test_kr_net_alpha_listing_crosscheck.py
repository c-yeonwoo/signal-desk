"""Small split controls; never empirical coverage certificates."""

import pytest

from scripts.measure.kr_net_alpha_listing_crosscheck import listing_fields

BINDING = {"ticker": "035720", "security_id": "KR7035720002"}
NOTICE = """카카오 변경상장(액면분할) 기명식 보통주 88,761,861주 (제201회) → 443,809,305주 (제202회)
변경주식수 : 355,047,444주 1주의 금액 : 500원 → 100원
② 발행일 기명식 보통주: 2021년04월14일 ③ 배당기산일 : 2021년01월01일
⑤ 변경상장일 : 2021년04월15일 ⑥ 변경사유 : 액면분할
표준코드 : KR7035720002 (단축코드:A035720)"""


def test_simple_split_has_distinct_dates_and_actual_not_planned_counts():
    result = listing_fields(NOTICE.encode(), BINDING)
    assert result["listed_shares_before"] == 88761861
    assert result["listed_shares_after"] == 443809305 and result["share_ratio"] == 5
    assert result["issued_on"] == "2021-04-14" and result["listed_on"] == "2021-04-15"
    assert result["holder_quantity_rule_verified"] and not result["economic_scope_certified"]
    assert "legal_effective_on" not in result  # issuance date alone is not legal effect proof


def test_split_rejects_inconsistent_counts_and_wrong_security():
    with pytest.raises(ValueError, match="arithmetic"):
        listing_fields(NOTICE.replace("443,809,305", "443,523,100").encode(), BINDING)
    with pytest.raises(ValueError, match="identity differs"):
        listing_fields(NOTICE.encode(), {"ticker": "005930", "security_id": "KR7005930003"})


def test_complex_or_multi_class_notice_does_not_inherit_simple_split_ratio():
    result = listing_fields(NOTICE.replace("변경사유 : 액면분할", "변경사유 : 회사분할").encode(), BINDING)
    assert result["identity_bound"] and not result["holder_quantity_rule_verified"]
    assert result["observed_ordinary_counts"] == [88761861, 443809305]
    assert result["observed_listing_on"] == "2021-04-15"
    assert "share_ratio" not in result
    result = listing_fields((NOTICE+" 우선주").encode(), BINDING)
    assert not result["holder_quantity_rule_verified"] and "share_ratio" not in result


def test_raw_share_transition_uses_previous_market_snapshot(tmp_path, monkeypatch):
    import json
    from scripts.measure import kr_net_alpha_listing_crosscheck as audit
    from scripts.measure.acquire_historical_sources import write_archive
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "DEST", tmp_path)
    folder = tmp_path/"stk_bydd_trd"; folder.mkdir()
    for day, shares in (("2021-04-14", 88761861), ("2021-04-15", 443809305)):
        raw = json.dumps({"OutBlock_1": [{"BAS_DD": day.replace("-", ""), "ISU_CD": "035720",
            "LIST_SHRS": str(shares)}]}).encode()
        write_archive(folder/(day+".zip"), {"response.json": raw}, {"entries": {
            "response.json": {"sha256": audit.digest(raw), "bytes": len(raw)}}})
    fields = listing_fields(NOTICE.encode(), BINDING)
    assert audit.raw_split_transition(fields)["matches_original"]
    fields["listed_shares_before"] = 88704620  # stale proposal, not actual observation
    result = audit.raw_split_transition(fields)
    assert not result["matches_original"] and not result["economic_scope_certified"]
    monkeypatch.setattr(audit.market_clock, "previous_session", lambda market, day: "2021-04-13")
    with pytest.raises(FileNotFoundError):
        audit.raw_split_transition(fields)  # cannot silently substitute an older available snapshot
