"""Offline public-query and row boundaries, not empirical scope certificates."""

import io
import xml.etree.ElementTree as ET

import pytest

from scripts.measure import kr_action_source_audit as transport
from scripts.measure.kr_net_alpha_seibro_scope import parse_response, ordinary, partition_check
from test_kr_action_source_audit import collector


def test_public_select_is_reserved_cached_and_never_an_action_free_certificate(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    calls = []

    class Response(io.BytesIO):
        code, headers = 200, {}

    class Opener:
        def open(self, request, timeout):
            assert c.journal[-1]["state"] == "reserved"
            assert request.full_url == transport.SEIBRO_CALENDAR
            assert request.get_header("Content-type") == "application/xml; charset=UTF-8"
            assert ET.fromstring(request.data).get("action") == transport.SEIBRO_ACTION
            calls.append(request)
            return Response(b'<vector result="0"/>')

    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *args: Opener())
    first, meta = c.seibro_calendar("2021-04-12", "2021-04-18")
    assert c.seibro_calendar("2021-04-12", "2021-04-18")[0] == first
    assert len(calls) == 1 and len(c.journal) == 1
    assert meta["calendar_query"]["DT_TPCD"] == "03"
    parsed = parse_response(first)
    assert parsed["rows"] == [] and not parsed["economic_scope_certified"]


@pytest.mark.parametrize("start,end,issuer", [
    ("2021-04-12","2021-04-19",""),  # UI forbids a seven-day difference without issuer.
    ("2020-01-01","2021-01-01","3572"),
    ("2019-12-31","2020-01-01",""),
    ("2021-04-12","2021-04-18","<write/>")])
def test_range_and_issuer_limits_are_checked_before_io(tmp_path, monkeypatch, start,end,issuer):
    c = collector(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="bounds"):
        c.seibro_calendar(start,end,issuer=issuer)
    assert c.journal == []


def test_only_observed_read_only_detail_actions_are_allowed(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    for action,reason in (("deleteUser","103"),("payDetailsList1","201")):
        with pytest.raises(ValueError, match="operation|bounds"):
            c.seibro_detail(action,"593","2021-03-31",reason)
    root = ET.Element("reqParam", {"action":"divSkedulView", "task":"ksd.private.UserPTask"})
    with pytest.raises(ValueError, match="operation"):
        c.get(transport.SEIBRO_CALENDAR, ET.tostring(root))
    with pytest.raises(ValueError, match="service key destination"):
        c.get(transport.SEIBRO_CALENDAR,
            b'<reqParam action="divSkedulView" task="ksd.safe.bip.cnts.Company.process.EntrSkedulPTask">'
            b'<ISSUCO_CUSTNO value="593"/><RGT_STD_DT value="20210331"/><RGT_RACD value="103"/></reqParam>',
            _service_key="SYNTHETIC")
    assert c.journal == []


def test_xml_count_class_and_unknown_values_are_not_guessed():
    raw = b'<vector result="1"><data><result><SHOTN_ISIN value="005935"/><STK_KACD value="preferred"/></result></data></vector>'
    parsed = parse_response(raw)
    assert ordinary(parsed["rows"],"005930")["state"] == "ordinary_share_unresolved"
    with pytest.raises(ValueError, match="count differs"):
        parse_response(raw.replace(b'result="1"',b'result="2"'))
    with pytest.raises(ValueError, match="declared result"):
        parse_response(b'<error>Service unavailable</error>')
    with pytest.raises(ValueError, match="shape ambiguous"):
        parse_response(b'<result result="1"><AMOUNT value="361"/><AMOUNT value="360"/></result>')
    with pytest.raises(ValueError, match="unrecognized records"):
        parse_response(b'<vector result="0"><error/></vector>')


def test_partition_check_ignores_only_query_clock_and_preserves_date_roles():
    a = {"SCH_DTTM":"20261018120000", "DT_BEGIN_DT":"20210705", "RGT_STD_DT":"20210630", "DT_TPCD":"34"}
    b = {**a,"DT_BEGIN_DT":"20210708", "DT_TPCD":"35"}
    same = {**a,"SCH_DTTM":"20261018120100"}
    assert partition_check([a],[same])["consistent"]
    result = partition_check([a,b],[same])
    assert not result["consistent"] and result["whole_unique_rows"] == 2 and result["partition_unique_rows"] == 1
    assert result["whole_only_rows"] == [{k:v for k,v in b.items() if k != "SCH_DTTM"}]
    assert not result["economic_scope_certified"]
