"""The filing clock and amount must come from source rows, never later prices."""

import pytest

from scripts.measure.contract_event_sources import original_document, parse_contract, parse_index


def _contract(amount="100,000,000", sales="1,000,000,000", ratio="10.00", related="-"):
    rows = [("2. 계약내역", "계약금액(원)", amount), ("최근매출액(원)", sales),
            ("매출액대비(%)", ratio), ("3. 계약상대", "구매 회사"), ("※ 관련공시", related)]
    return "<table>" + "".join("<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
                               for row in rows) + "</table>"


def test_contract_amount_is_issuer_basis_and_consistent_with_reported_ratio():
    facts = parse_contract(_contract())
    assert facts["amount_krw"] == 100_000_000
    assert facts["revenue_ratio_pct"] == 10
    assert facts["counterparty"] == "구매 회사"
    assert facts["issuer_role"] == "seller_or_supplier"
    with pytest.raises(ValueError, match="ratio differ"):
        parse_contract(_contract(ratio="50"))
    with pytest.raises(ValueError, match="earlier related"):
        parse_contract(_contract(related="2026-04-01 기존 공시"))


def test_ambiguous_units_are_not_used_as_positive_contract_amount():
    with pytest.raises(ValueError, match="unit ambiguous"):
        parse_contract(_contract(amount="100백만원"))


def test_index_clock_is_official_minute_and_future_correction_badge_is_not_title():
    source = """전체 <em>1</em>건 <tr><td>1</td><td>2026-06-03 10:15</td>
    <td><a onclick="companysummary_open('00593')">삼성전자</a></td>
    <td><a onclick="openDisclsViewer('20260603000123','')">단일판매ㆍ공급계약체결
    <img alt='해당보고서 이후에 정정된 보고서 있음'></a></td><td>삼성전자</td></tr>"""
    total, rows = parse_index(source)
    assert total == 1 and rows[0]["ticker"] == "005930"
    assert rows[0]["published_at"] == "2026-06-03T10:15:00+09:00"
    assert "정정" not in rows[0]["title"]
    assert parse_index(source.replace("00593", "0011T"))[1][0]["kind_company_code"] == "0011T"
    assert parse_index(source.replace("00593", "CYM06"))[1][0]["kind_company_code"] == "CYM06"
    with pytest.raises(ValueError, match="outside the requested"):
        parse_index(source.replace("2026-06-03 10:15", "2026-08-06 10:15"))


def test_document_number_date_is_not_the_publication_date():
    shell = """<option value='20260428000985|Y'selected='selected'>
    단일판매ㆍ공급계약체결 (2026.05.04)</option>"""
    assert original_document(shell, {"session": "2026-05-04"}) == "20260428000985"
    with pytest.raises(ValueError, match="missing or ambiguous"):
        original_document(shell, {"session": "2026-04-28"})
