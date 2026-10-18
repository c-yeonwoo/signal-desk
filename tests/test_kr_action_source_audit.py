"""Offline source-boundary regressions, not empirical economic certificates."""

import io
import json
import urllib.error

import pytest

from scripts.measure import kr_action_source_audit as audit
from scripts.measure.kr_net_alpha_v2 import price_request_inventory


def html(rows, total=None):
    return "<em>unused</em>"+"".join(
        "<tr><td>1</td><td>2021-03-29 15:55</td><td>"+
        (f"<a onclick=\"companysummary_open('{code}')\">issuer</a>" if code else "")+
        f"</td><td><a onclick=\"openDisclsViewer('{receipt}','')\">{title}</a></td>"
        "<td>유가증권시장본부</td></tr>" for code, receipt, title in rows
    )+f"전체 <em>{len(rows) if total is None else total}</em>건"


def test_market_wide_not_dropped_and_preferred_not_rebound_to_common():
    total, rows = audit.listing(html([(None, "20210329001191", "배당락 기준가격 안내"),
        ("00022", "20210329001173", "권리락 기준가격 안내(유유제약1우)")]), "2021-03-01", "2021-03-31")
    assert total == 2 and rows[0]["company_code"] is None
    assert rows[1]["company_code"] == "00022" and "1우" in rows[1]["title"]
    assert all("security_id" not in r and "ticker" not in r for r in rows)


def test_out_of_scope_and_missing_census_rejected():
    with pytest.raises(ValueError, match="requested dates"):
        audit.listing(html([(None, "20210329001191", "notice")]), "2021-04-01", "2021-04-30")
    with pytest.raises(ValueError, match="total missing"):
        audit.listing("<html>HTTP 200 error</html>", "2021-03-01", "2021-03-31")


class FakeCollector:
    spec = {"warmup_from": "2020-01-01", "liquidation_through": "2024-01-31"}

    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, url, data=None):
        return next(self.responses).encode(), {"sha256": "fixture", "response_path": "fixture.htm"}


def test_page_cap_and_duplicate_receipts_do_not_certify_complete_list():
    row = (None, "20210329001191", "notice")
    report = audit.census(FakeCollector([html([row], 101)]), {}, "2021-03-01", "2021-03-31", 1)
    assert report["state"] == "listing_incomplete"
    duplicate = audit.census(FakeCollector([html([row, row])]), {}, "2021-03-01", "2021-03-31")
    assert duplicate["state"] == "listing_incomplete"
    complete = audit.census(FakeCollector([html([row])]), {}, "2021-03-01", "2021-03-31")
    assert complete["state"] == "listing_complete_not_economic_scope"
    assert complete["all_corporate_actions_certified"] is False


def test_empty_list_never_certifies_no_events():
    report = audit.census(FakeCollector([html([])]), {}, "2021-03-01", "2021-03-31")
    assert report["declared_total"] == 0
    assert report["all_corporate_actions_certified"] is False
    assert report["correction_chain_reviewed"] is False


def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DEST", tmp_path)
    monkeypatch.setattr(audit, "source_storage_bytes", lambda: 0)
    monkeypatch.setattr(audit, "contract", lambda: {
        "amendment": {"source_scope": {"previous_action_requests": 7, "previous_price_metadata_requests": 808}},
        "max_action_source_requests": 400, "max_price_metadata_requests": 1200,
        "max_new_archive_bytes": 100000, "warmup_from": "2020-01-01", "liquidation_through": "2024-01-31"})
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: None)
    return audit.Collector()


def test_http_error_body_reserved_and_preserved_no_retry(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)

    class Opener:
        def open(self, request, timeout):
            assert json.loads(c.path.read_text())[-1]["state"] == "reserved"
            raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, io.BytesIO(b"not a certificate"))

    monkeypatch.setattr(audit.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="HTTP 403"):
        c.get("https://kind.krx.co.kr/test")
    with pytest.raises(ValueError, match="preserved source unavailable"):
        c.get("https://kind.krx.co.kr/test")
    assert len(c.journal) == 1
    assert (tmp_path/c.journal[0]["response_path"]).read_bytes() == b"not a certificate"
    assert c.journal[0]["state"] == "http_error"


@pytest.mark.parametrize("url", ["http://kind.krx.co.kr/test", "https://example.com/",
    "https://kind.krx.co.kr/?serviceKey=secret", "https://user:pw@kind.krx.co.kr/"])
def test_unapproved_requests_rejected_before_io(tmp_path, monkeypatch, url):
    c = collector(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="outside read-only"):
        c.get(url)
    assert c.journal == []


def test_reservation_and_cached_path_safety_and_budget(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    c.spec["max_action_source_requests"] = 7
    with pytest.raises(ValueError, match="request cap"):
        c.get("https://kind.krx.co.kr/test")
    c.spec["max_action_source_requests"] = 400
    key = audit.hashlib.sha256(b"https://kind.krx.co.kr/test\0").hexdigest()
    c.journal = [{"request_key": key, "response_path": "../outside"}]
    with pytest.raises(ValueError, match="outside research"):
        c.get("https://kind.krx.co.kr/test")


def test_original_does_not_silently_choose_latest_correction():
    shell = ('<option value="20210329000001|Y">notice (2021.03.29)</option>'
             '<option value="20210329000002|Y">[정정]notice (2021.03.29)</option>')
    with pytest.raises(ValueError, match="version ambiguous"):
        audit.original(FakeCollector([shell]), "20210329001191")


def test_original_binds_receipt_path_but_not_prepared_document_date():
    shell = '<option value="20210328000001|Y">notice (2021.03.29)</option>'
    url = "https://kind.krx.co.kr/external/2021/03/29/001191/20210328000001/99342.htm"
    raw, meta = audit.original(FakeCollector([shell, url, "original bytes"]), "20210329001191")
    assert raw == b"original bytes" and meta["document_number"] == "20210328000001"
    assert meta["correction_chain_reviewed"] is False
    with pytest.raises(ValueError, match="original unresolved"):
        audit.original(FakeCollector([shell, url.replace("001191/", "001192/")]), "20210329001191")


def test_failed_supplementary_price_requests_count_without_rewriting_parent(tmp_path):
    parent = b'[{"state":"response_saved"},{"state":"interrupted"}]'
    (tmp_path/"requests.json").write_bytes(parent)
    assert price_request_inventory(tmp_path)["price_metadata_attempts"] == 2
    (tmp_path/"v2-price-requests.json").write_text('[{"state":"failed"}]')
    result = price_request_inventory(tmp_path)
    assert result["price_metadata_attempts"] == 3
    assert result["original_price_metadata_attempts"] == 2
    assert result["supplementary_price_metadata_attempts"] == 1
    assert (tmp_path/"requests.json").read_bytes() == parent


def test_kis_probe_preserves_raw_mode_and_counts_before_io(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    monkeypatch.setattr(audit.config, "kis_credentials", lambda: {"env": "demo"})

    def request(endpoint, tr_id, params, creds, cont):
        assert params["FID_ORG_ADJ_PRC"] == "1"
        assert creds["env"] == "demo"
        assert json.loads((tmp_path/"v2-price-requests.json").read_text())[-1]["state"] == "reserved"
        body = {"rt_cd": "0", "output2": [{"flng_cls_code": "00", "prtt_rate": "0.00"}]}
        return json.dumps(body).encode(), body, ""

    monkeypatch.setattr(audit, "_fetch_kis_schedule_page", request)
    raw, row = c.kis_flags("035720", "2021-04-01", "2021-04-30")
    assert row["economic_scope_certified"] is False
    assert json.loads(raw)["output2"][0]["flng_cls_code"] == "00"
    assert c.kis_flags("035720", "2021-04-01", "2021-04-30")[0] == raw
    assert len(json.loads((tmp_path/"v2-price-requests.json").read_text())) == 1


def test_fsc_key_is_transmitted_only_to_fixed_api_and_never_journaled(tmp_path, monkeypatch):
    import urllib.parse
    c = collector(tmp_path, monkeypatch)
    secret = "SYNTHETIC-KEY+/="
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", urllib.parse.quote(secret, safe=""))
    monkeypatch.setattr(audit.config, "load_env", lambda: None)

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith(audit.FSC_RIGHTS+"?")
            assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["serviceKey"] == [secret]
            assert secret not in c.path.read_text()
            response = io.BytesIO(b'{"response":{"header":{"resultCode":"00"}}}')
            response.code, response.headers = 200, {}
            return response
    monkeypatch.setattr(audit.urllib.request, "build_opener", lambda *args: Opener())
    c.fsc_rights("2021-04-15")
    assert secret not in c.path.read_text() and "serviceKey=" not in c.path.read_text()
    with pytest.raises(ValueError, match="destination"):
        c.get("https://kind.krx.co.kr/test", _service_key=secret)
    assert len(c.journal) == 1


def test_fsc_credential_echo_is_not_saved_and_failure_counts(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    secret = "SYNTHETIC-DO-NOT-SAVE"
    class Opener:
        def open(self, request, timeout):
            response = io.BytesIO(secret.encode()); response.code, response.headers = 200, {}
            return response
    monkeypatch.setattr(audit.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="credential echoed"):
        c.get(audit.FSC_RIGHTS+"?basDt=20210415", _service_key=secret)
    assert c.journal[0]["state"] == "credential_echo_not_saved"
    assert not c.journal[0].get("response_path") and secret not in c.path.read_text()


def test_fsc_historical_page_is_not_an_economic_certificate():
    from scripts.measure.kr_net_alpha_fsc_rights import parse_response
    body = {"response": {"header": {"resultCode": "00"}, "body": {"pageNo": 1,
        "numOfRows": 100, "totalCount": 101, "items": {"item": [{"basDt": "20210415"}]}}}}
    result = parse_response(json.dumps(body).encode(), "2021-04-15")
    assert result["state"] == "page_received"
    assert not result["economic_scope_certified"] and not result["all_pages_received"]
    body["response"]["body"]["items"]["item"][0]["basDt"] = "20231228"
    with pytest.raises(ValueError, match="basis date"):
        parse_response(json.dumps(body).encode(), "2021-04-15")


def test_fsc_empty_and_rejected_responses_are_distinct():
    from scripts.measure.kr_net_alpha_fsc_rights import parse_response
    body = {"response": {"header": {"resultCode": "00"}, "body": {"pageNo": 1,
        "numOfRows": 100, "totalCount": 0, "items": ""}}}
    assert parse_response(json.dumps(body).encode(), "2021-04-15")["state"] == "empty_not_action_free"
    raw = b'<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>'
    assert parse_response(raw, "2021-04-15")["state"] == "api_rejected"
    raw = json.dumps({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
        "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnReasonCode": "30"}}}).encode()
    result = parse_response(raw, "2021-04-14")
    assert result["state"] == "api_rejected" and result["result_code"] == "30"


def test_fsc_issuance_endpoint_remains_bounded_and_uses_same_redacted_transport(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "SYNTHETIC-ISSUANCE")
    monkeypatch.setattr(audit.config, "load_env", lambda: None)
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return b"fixture", {}
    monkeypatch.setattr(c, "get", get)
    c.fsc_issuance("2021-04-14", issuer="카카오")
    assert calls[0][0].startswith(audit.FSC_ISSUANCE+"?")
    assert "SYNTHETIC" not in calls[0][0]
    assert calls[0][1]["_service_key"] == "SYNTHETIC-ISSUANCE"
    with pytest.raises(ValueError, match="fixed research scope"):
        c.fsc_issuance("2025-04-14", issuer="카카오")
    assert len(calls) == 1


def test_fsc_explicit_recheck_preserves_rejection_and_counts_new_attempt(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            response = io.BytesIO(b"rejected" if len(calls) == 1 else b"accepted")
            response.code, response.headers = (403 if len(calls) == 1 else 200), {}
            return response
    monkeypatch.setattr(audit.urllib.request, "build_opener", lambda *args: Opener())
    url = audit.FSC_ISSUANCE+"?basDt=20210414"
    first, first_meta = c.get(url, _service_key="SYNTHETIC")
    assert c.get(url, _service_key="SYNTHETIC")[0] == first and len(calls) == 1
    assert c.get(url, _service_key="SYNTHETIC", _refresh=True)[0] == b"accepted"
    assert len(c.journal) == 2 and len(calls) == 2
    assert (tmp_path/first_meta["response_path"]).read_bytes() == b"rejected"
    assert c.get(url, _service_key="SYNTHETIC")[0] == b"accepted" and len(calls) == 2
    with pytest.raises(ValueError, match="explicit recheck"):
        c.get("https://kind.krx.co.kr/test", _refresh=True)


def test_fsc_exact_issuer_match_preserves_distinct_date_roles_and_face_variants():
    from scripts.measure.kr_net_alpha_fsc_rights import issuer_roles
    row = {"stckIssuCmpyNm": "카카오", "scrsIssuMnbdCd": "03572", "stckIssuRcd": "split",
        "rgtExertRcd": "ex", "stckIssuRcdNm": "액면분할", "rgtExertRcdNm": "권리락일",
        "rgtExertSttgDt": "20210413", "rgtExertEdDt": "20210413", "stckParPrc": "100"}
    result = issuer_roles([row, dict(row, stckParPrc="500"),
        dict(row, stckIssuCmpyNm="카카오뱅크"), dict(row, scrsIssuMnbdCd="other")], "카카오", "03572")
    assert result["selected_rows"] == 2
    assert len(result["roles"]) == 1 and len(result["roles"][0]["observations"]) == 2
    assert result["roles"][0]["observations"][0]["start_on"] == "20210413"
    assert not result["share_class_verified"] and not result["economic_scope_certified"]
    assert not result["payment_completed_verified"]


def test_fsc_pagination_rejects_changing_counts_and_incomplete_pages():
    from scripts.measure.kr_net_alpha_fsc_rights import schedule_pages
    class Pages:
        def __init__(self, totals, sizes):
            self.totals, self.sizes = totals, sizes
        def fsc_rights(self, day, page, issuer):
            rows = [{"basDt": "20210415", "id": f"{page}-{i}"} for i in range(self.sizes[page-1])]
            return json.dumps({"response": {"header": {"resultCode": "00"}, "body": {
                "pageNo": page, "numOfRows": 100, "totalCount": self.totals[page-1],
                "items": {"item": rows}}}}).encode(), {"http_status": 200}
    report, _ = schedule_pages(Pages([101], [100]), "2021-04-15", max_pages=1)
    assert not report["complete_query"] and not report["economic_scope_certified"]
    with pytest.raises(ValueError, match="count changed"):
        schedule_pages(Pages([101, 102], [100, 2]), "2021-04-15")
    with pytest.raises(ValueError, match="page incomplete"):
        schedule_pages(Pages([101], [1]), "2021-04-15")
    report, _ = schedule_pages(Pages([101, 101], [100, 1]), "2021-04-15")
    assert report["complete_query"] and not report["economic_scope_certified"]
