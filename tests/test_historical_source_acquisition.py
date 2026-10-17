import json
import io
import datetime as dt
from zipfile import ZipFile

import pytest

from scripts.measure import acquire_historical_sources as acquisition
from signal_desk.signals.research_action_coverage import ActionRecord, AvailabilityEvidence


def test_krx_acquisition_preserves_raw_sessions_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition.config, "krx_key", lambda: "test-key")
    monkeypatch.setattr(acquisition.time, "sleep", lambda seconds: None)
    requested = []

    def fetch(url, params, headers, source):
        assert source == "KRX" and headers == {"AUTH_KEY": "test-key"}
        day = params["basDd"]
        requested.append(day)
        body = {"OutBlock_1": [{"BAS_DD": day, "ISU_CD": "005930", "ISU_NM": "삼성전자",
                                 "TDD_CLSPRC": "100", "MKTCAP": "100000"}]}
        return json.dumps(body).encode(), body

    monkeypatch.setattr(acquisition, "_fetch_json", fetch)
    files, manifest = acquisition.acquire_krx("2026-07-30", "2026-07-31")
    assert requested == ["20260730", "20260731"]
    assert manifest["request_count"] == 2 and manifest["strict_pit_eligible"] is False
    output = tmp_path / "source.zip"
    acquisition.write_archive(output, files, manifest)
    with ZipFile(output) as archive:
        assert archive.read("raw/2026-07-30.json") == files["raw/2026-07-30.json"]
        saved = json.loads(archive.read("manifest.json"))
        assert saved["entries"]["raw/2026-07-30.json"]["sha256"] == manifest["entries"]["raw/2026-07-30.json"]["sha256"]
        assert b"test-key" not in archive.read("manifest.json")
    with pytest.raises(FileExistsError):
        acquisition.write_archive(output, files, manifest)
    with pytest.raises(ValueError, match="registered period"):
        acquisition.acquire_krx("2026-08-04", "2026-08-05")
    assert requested == ["20260730", "20260731"]


def test_registered_boundary_compares_dates_not_input_string_format():
    assert acquisition._range("20251221", "20260120") == (
        dt.date(2025, 12, 21), dt.date(2026, 1, 20))
    with pytest.raises(ValueError, match="registered period"):
        acquisition._range("20260804", "20260805")


def test_dart_acquisition_retains_corrections_and_checks_all_pages(monkeypatch):
    monkeypatch.setattr(acquisition.config, "dart_key", lambda: "test-secret")
    monkeypatch.setattr(acquisition.time, "sleep", lambda seconds: None)
    seen = []

    def fetch(url, params, headers, source):
        assert source == "DART" and params["last_reprt_at"] == "N"
        assert params["crtfc_key"] == "test-secret"
        assert params["pblntf_detail_ty"] == "A001"
        page = int(params["page_no"])
        seen.append(page)
        start, end = (1, 101) if page == 1 else (101, 102)
        rows = [{"rcept_no": f"20260331{n:06d}", "rcept_dt": "20260331",
                 "stock_code": "005930", "report_nm": "사업보고서"} for n in range(start, end)]
        body = {"status": "000", "total_count": "101", "total_page": "2", "list": rows}
        return json.dumps(body).encode(), body

    monkeypatch.setattr(acquisition, "_fetch_json", fetch)
    files, manifest = acquisition.acquire_dart("2026-03-01", "2026-03-31", "A001")
    assert seen == [1, 2]
    assert len(files) == 2 and manifest["distinct_receipts"] == 101
    assert "crtfc_key" not in manifest["query"]
    assert manifest["strict_pit_eligible"] is False

    def duplicate(url, params, headers, source):
        body = {"status": "000", "total_count": "2", "total_page": "1",
                "list": [{"rcept_no": "20260331000001", "rcept_dt": "20260331"}] * 2}
        return json.dumps(body).encode(), body

    monkeypatch.setattr(acquisition, "_fetch_json", duplicate)
    with pytest.raises(acquisition.SourceError, match="duplicate receipt"):
        acquisition.acquire_dart("2026-03-01", "2026-03-31", "A001")


def test_source_error_never_prints_credential_url(monkeypatch):
    def bad_request(request, timeout):
        raise RuntimeError("secret query would be in URL")

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", bad_request)
    with pytest.raises(acquisition.SourceError) as error:
        acquisition._fetch_json(acquisition.DART_URL, {"crtfc_key": "hidden"}, {}, "DART")
    assert "hidden" not in str(error.value)
    assert "secret query" not in str(error.value)


def test_kind_review_preserves_selected_originals_without_claiming_completeness(tmp_path, monkeypatch):
    url = "https://kind.krx.co.kr/external/2024/06/25/000570/20240625001567/12000.htm"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema": "research-kind-review-plan-v1", "documents": [
        {"id": "ssangyong-close", "url": url, "expected_text": "쌍용씨앤이"}]}), encoding="utf-8")
    raw = "<HTML>쌍용씨앤이 주식교환 종료보고서</HTML>".encode()

    class Response:
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return url

        def read(self, count):
            assert count == 10_000_001
            return raw

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda request, timeout: Response())
    files, manifest = acquisition.acquire_kind_reviewed(plan)
    assert manifest["source_state"] == "selected_originals_not_action_complete"
    assert manifest["strict_pit_eligible"] is False
    output = tmp_path / "kind.zip"
    acquisition.write_archive(output, files, manifest)
    checked = acquisition.verify_action_archive(output)
    assert checked["raw_members"] == 1 and checked["total_return_coverage"] is False
    with pytest.raises(FileExistsError):
        acquisition.write_archive(output, files, manifest)


def test_kind_review_rejects_wrong_identity_redirect_and_protected_date(tmp_path, monkeypatch):
    url = "https://kind.krx.co.kr/external/2024/06/25/000570/20240625001567/12000.htm"
    plan = tmp_path / "plan.json"
    item = {"id": "ssangyong-close", "url": url, "expected_text": "쌍용씨앤이"}
    plan.write_text(json.dumps({"schema": "research-kind-review-plan-v1",
                                "documents": [item]}), encoding="utf-8")

    class Response:
        headers = {"Content-Type": "text/html"}

        def __init__(self, final_url, raw):
            self.final_url, self.raw = final_url, raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return self.final_url

        def read(self, count):
            return self.raw

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda request, timeout:
                        Response(url, b"<HTML>different issuer</HTML>"))
    with pytest.raises(acquisition.SourceError, match="identity"):
        acquisition.acquire_kind_reviewed(plan)
    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda request, timeout:
                        Response("https://elsewhere.example/", b"<HTML>different issuer</HTML>"))
    with pytest.raises(acquisition.SourceError, match="redirected"):
        acquisition.acquire_kind_reviewed(plan)
    item["url"] = "https://kind.krx.co.kr/external/2026/08/05/000570/20260805001567/12000.htm"
    plan.write_text(json.dumps({"schema": "research-kind-review-plan-v1",
                                "documents": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="date"):
        acquisition.acquire_kind_reviewed(plan)


def test_kind_listing_history_archives_only_page_with_identity_date_and_type(tmp_path, monkeypatch):
    url = ("https://kind.krx.co.kr/listinvstg/listinvstgcom.do?"
           "bizProcNo=20240726000244&method=searchListInvstgCorpDetail")
    plan = tmp_path / "listing-plan.json"
    item = {"id": "lgcns-20250205", "url": url, "expected_text": "LG씨엔에스",
            "expected_listing_date": "2025-02-05"}
    plan.write_text(json.dumps({"schema": "research-kind-listing-plan-v1",
                                "documents": [item]}), encoding="utf-8")

    class Response:
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return url

        def read(self, count):
            assert count == 1_000_001
            return self.body

    good = "<html>LG씨엔에스 신규상장 2025-02-05</html>".encode()
    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda request, timeout: Response(good))
    files, manifest = acquisition.acquire_kind_listing_reviewed(plan)
    output = tmp_path / "listing.zip"
    acquisition.write_archive(output, files, manifest)
    assert acquisition.verify_action_archive(output)["raw_members"] == 1
    assert manifest["strict_pit_eligible"] is False

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda request, timeout:
                        Response(b"<html>JavaScript loading shell</html>"))
    with pytest.raises(acquisition.SourceError, match="identity, date or listing type"):
        acquisition.acquire_kind_listing_reviewed(plan)
    item["url"] = "https://kind.krx.co.kr/listinvstg/listinvstgcom.do?bizProcNo=20240726000244&method=unsafe"
    plan.write_text(json.dumps({"schema": "research-kind-listing-plan-v1",
                                "documents": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or duplicate"):
        acquisition.acquire_kind_listing_reviewed(plan)


def test_transient_invalid_json_is_retried_without_archiving_it(monkeypatch):
    calls = []

    class Response:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.raw

    def request(_request, timeout):
        calls.append(timeout)
        return Response(b"{") if len(calls) == 1 else Response(b'{"status":"000"}')

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", request)
    monkeypatch.setattr(acquisition.time, "sleep", lambda seconds: None)
    raw, body = acquisition._fetch_json(acquisition.DART_URL, {"crtfc_key": "hidden"}, {}, "DART")
    assert calls == [30, 30] and raw == b'{"status":"000"}' and body["status"] == "000"


def test_dart_action_archive_retains_every_filing_and_original(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition.config, "dart_key", lambda: "test-secret")
    receipts = ("20240430800061", "20240430800062")
    seen = []
    monkeypatch.setattr(acquisition.time, "sleep", lambda seconds: seen.append(("gap", seconds)))

    def listing(url, params, headers, source):
        assert source == "DART actions" and params["corp_code"] == "00126380"
        assert params["last_reprt_at"] == "N" and "pblntf_ty" not in params
        seen.append(("list", params["page_no"]))
        body = {"status": "000", "total_count": "2", "total_page": "1", "list": [
            {"corp_code": "00126380", "stock_code": "005930",
             "rcept_no": receipt, "rcept_dt": "20240430"}
            for receipt in receipts]}
        return json.dumps(body).encode(), body

    def original(receipt, key):
        assert key == "test-secret"
        seen.append(("doc", receipt))
        blob = io.BytesIO()
        with ZipFile(blob, "w") as archive:
            archive.writestr(receipt + ".xml", (
                "<html><style>3. 1주당 배당금(원) 보통주식 999</style><body>"
                "3. 1주당 배당금(원) 보통주식 361 종류주식 361 "
                "6. 배당기준일 2024-03-31 "
                "7. 배당금지급 예정일자 2024-05-20 "
                "10. 이사회결의일(결정일) 2024-04-30"
                "</body></html>").encode())
        return blob.getvalue()

    monkeypatch.setattr(acquisition, "_fetch_json", listing)
    monkeypatch.setattr(acquisition, "_fetch_dart_document", original)
    files, manifest = acquisition.acquire_dart_actions("00126380", "2024-04-30", "2024-04-30", gap_seconds=1)
    assert seen == [("list", "1"), ("gap", 1), ("doc", receipts[0]), ("gap", 1), ("doc", receipts[1])]
    assert manifest["source_state"] == "filings_archived_not_action_complete"
    assert manifest["total_return_coverage"] is False and len(files) == 3
    output = tmp_path / "dart-actions.zip"
    acquisition.write_archive(output, files, manifest)
    assert acquisition.verify_action_archive(output)["raw_members"] == 3
    window = acquisition.source_window_from_archive(output)
    assert window.date_basis == "receipt" and window.historical_range_confirmed is False
    assert window.security_id == "corp:00126380"
    with pytest.raises(acquisition.SourceError, match="missing, corrected"):
        acquisition.extract_dart_common_dividend(output, receipts[0])

    body = json.loads(files["raw/list/page-001.json"])
    body["list"][0]["report_nm"] = "현금ㆍ현물배당결정"
    new_raw = json.dumps(body).encode()
    amended_files = dict(files)
    amended_files["raw/list/page-001.json"] = new_raw
    amended_manifest = dict(manifest)
    amended_manifest["entries"] = dict(manifest["entries"])
    amended_manifest["entries"]["raw/list/page-001.json"] = acquisition._entry(new_raw, rows=2)
    amended_output = tmp_path / "typed.zip"
    acquisition.write_archive(amended_output, amended_files, amended_manifest)
    fact = acquisition.extract_dart_common_dividend(amended_output, receipts[0])
    assert fact.cash_per_share == 361 and fact.record_date == "2024-03-31"
    assert fact.scheduled_payment_date == "2024-05-20"
    assert fact.effective_session is None and fact.actual_payment_date is None
    assert fact.availability.availability_kind == "retrospective"

    blank_payment_files = dict(amended_files)
    blank_original = io.BytesIO()
    with ZipFile(blank_original, "w") as blank_zip:
        blank_zip.writestr(receipts[0] + ".xml", (
            "<body>3. 1주당 배당금(원) 보통주식 361 종류주식 - "
            "6. 배당기준일 2024-03-31 "
            "7. 배당금지급 예정일자 - 8. 주주총회 개최여부 미개최 "
            "10. 이사회결의일(결정일) 2024-04-30</body>"
        ).encode())
    blank_name = f"raw/doc/{receipts[0]}.zip"
    blank_payment_files[blank_name] = blank_original.getvalue()
    blank_payment_manifest = dict(amended_manifest)
    blank_payment_manifest["entries"] = dict(amended_manifest["entries"])
    blank_payment_manifest["entries"][blank_name] = acquisition._entry(
        blank_payment_files[blank_name], rows=1)
    blank_payment_output = tmp_path / "blank-payment.zip"
    acquisition.write_archive(blank_payment_output, blank_payment_files,
                              blank_payment_manifest)
    blank_fact = acquisition.extract_dart_common_dividend(
        blank_payment_output, receipts[0])
    assert blank_fact.scheduled_payment_date is None
    assert blank_fact.actual_payment_date is None

    tampered_files = dict(amended_files)
    tampered_files["raw/list/page-001.json"] = new_raw + b" "
    tampered = tmp_path / "tampered.zip"
    acquisition.write_archive(tampered, tampered_files, amended_manifest)
    with pytest.raises(acquisition.SourceError, match="hash/length"):
        acquisition.verify_action_archive(tampered)

    def changed(url, params, headers, source):
        raw, body = listing(url, params, headers, source)
        body["total_count"] = "3"
        return raw, body

    monkeypatch.setattr(acquisition, "_fetch_json", changed)
    with pytest.raises(acquisition.SourceError, match="incomplete or capped"):
        acquisition.acquire_dart_actions("00126380", "2024-04-30", "2024-04-30")
    with pytest.raises(acquisition.SourceError, match="incomplete or capped"):
        acquisition.acquire_dart_actions("00126380", "2024-04-30", "2024-04-30",
                                         max_docs=1)

    monkeypatch.setattr(acquisition, "_fetch_json", listing)
    monkeypatch.setattr(acquisition, "_fetch_dart_document",
                        lambda receipt, key: pytest.fail("list-only fetched a document"))
    listed, listing_manifest = acquisition.acquire_dart_actions(
        "00126380", "2024-04-30", "2024-04-30", list_only=True, max_docs=1)
    assert set(listed) == {"raw/list/page-001.json"}
    assert listing_manifest["source_state"] == "filing_list_only_not_action_complete"
    assert listing_manifest["request_count"] == 1
    listing_output = tmp_path / "list-only.zip"
    acquisition.write_archive(listing_output, listed, listing_manifest)
    assert acquisition.source_window_from_archive(listing_output).state == "partial"

    monkeypatch.setattr(acquisition, "_fetch_dart_document", original)
    selected, selected_manifest = acquisition.acquire_dart_actions(
        "00126380", "2024-04-30", "2024-04-30", max_docs=1,
        selected_receipts=(receipts[1],))
    assert set(selected) == {"raw/list/page-001.json", f"raw/doc/{receipts[1]}.zip"}
    assert selected_manifest["total_filings"] == 2
    assert selected_manifest["source_state"] == "selected_originals_not_action_complete"
    selected_output = tmp_path / "selected.zip"
    acquisition.write_archive(selected_output, selected, selected_manifest)
    assert acquisition.source_window_from_archive(selected_output).state == "partial"
    with pytest.raises(acquisition.SourceError, match="selected receipt absent"):
        acquisition.acquire_dart_actions("00126380", "2024-04-30", "2024-04-30",
                                         selected_receipts=("20240430809999",))


@pytest.mark.parametrize("members", [
    ["20260318000313.xml"],
    ["20260318000313_00760.xml"],
    ["20260318000313.xml", "20260318000313_00760.xml",
     "20260318000313_00761.xml"],
])
def test_dart_original_accepts_only_receipt_bound_main_or_attachment_xml(monkeypatch, members):
    payload = io.BytesIO()
    with ZipFile(payload, "w") as archive:
        for member in members:
            archive.writestr(member, b"<document>original</document>")
    monkeypatch.setattr(acquisition.urllib.request, "urlopen",
                        lambda request, timeout: io.BytesIO(payload.getvalue()))
    assert acquisition._fetch_dart_document("20260318000313", "secret") == payload.getvalue()


@pytest.mark.parametrize("members", [
    ["20260318000314_00760.xml"],
    ["20260318000313_0076.xml"],
    ["../20260318000313.xml"],
    [f"20260318000313_{index:05d}.xml" for index in range(21)],
])
def test_dart_original_rejects_wrong_or_excessive_attachment_xml(monkeypatch, members):
    payload = io.BytesIO()
    with ZipFile(payload, "w") as archive:
        for name in members:
            archive.writestr(name, b"<document>original</document>")
    monkeypatch.setattr(acquisition.urllib.request, "urlopen",
                        lambda request, timeout: io.BytesIO(payload.getvalue()))
    with pytest.raises(acquisition.SourceError, match="invalid archive members"):
        acquisition._fetch_dart_document("20260318000313", "secret")


def test_dart_document_014_is_not_an_empty_original(monkeypatch):
    monkeypatch.setattr(acquisition.urllib.request, "urlopen",
                        lambda request, timeout: io.BytesIO(
                            b"<result><status>014</status></result>"))
    with pytest.raises(acquisition.SourceError, match="response code 014"):
        acquisition._fetch_dart_document("20260319000350", "secret")


def test_dart_013_is_preserved_but_does_not_claim_action_free(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition.config, "dart_key", lambda: "test-secret")

    def no_filing(url, params, headers, source):
        body = {"status": "013", "message": "조회된 데이타가 없습니다."}
        return json.dumps(body).encode(), body

    monkeypatch.setattr(acquisition, "_fetch_json", no_filing)
    files, manifest = acquisition.acquire_dart_actions("00126380", "2024-03-10", "2024-03-10")
    assert manifest["source_state"] == "source_empty_not_action_free"
    assert manifest["total_return_coverage"] is False and manifest["total_filings"] == 0
    output = tmp_path / "empty.zip"
    acquisition.write_archive(output, files, manifest)
    assert acquisition.verify_action_archive(output)["source_state"] == "source_empty_not_action_free"
    with pytest.raises(acquisition.SourceError, match="selected receipt absent"):
        acquisition.acquire_dart_actions(
            "00126380", "2024-03-10", "2024-03-10",
            selected_receipts=("20240310800001",))


def test_kis_schedule_continuation_and_errors_are_not_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition.config, "kis_credentials", lambda: {"env": "real"})
    monkeypatch.setattr(acquisition.time, "sleep", lambda seconds: None)
    seen = []

    def page(path, tr_id, params, creds, tr_cont):
        seen.append(tr_cont)
        index = len(seen)
        body = {"rt_cd": "0", "output1": [{
            "sht_cd": "005930", "isin_name": "삼성전자", "stk_kind": "보통",
            "record_date": "20240331" if index == 1 else "20240630",
            "per_sto_divi_amt": "361", "divi_pay_dt": "2024/05/20"}]}
        return json.dumps(body).encode(), body, "M" if index == 1 else ""

    monkeypatch.setattr(acquisition, "_fetch_kis_schedule_page", page)
    files, manifest = acquisition.acquire_kis_schedule("dividend", "005930",
                                                        "2024-03-01", "2024-03-31")
    assert seen == ["", "N"] and len(files) == 2
    assert manifest["source_state"] == "schedule_archived_not_action_complete"
    assert manifest["total_return_coverage"] is False
    archive = tmp_path / "kis.zip"
    acquisition.write_archive(archive, files, manifest)
    assert acquisition.source_window_from_archive(archive).date_basis == "unknown"
    facts = acquisition.extract_kis_schedule_facts(archive)
    assert len(facts) == 2
    assert facts[0].cash_per_share == 361
    assert facts[0].availability.published_on is None
    assert facts[0].status == "scheduled" and facts[0].actual_payment_date is None
    observed_at = dt.datetime.now(dt.timezone.utc)
    dart = ActionRecord("kr", "ticker:005930", "DART:20240430800061:common",
                        "cash_dividend", None, "scheduled", "DART:20240430800061",
                        "a" * 64, AvailabilityEvidence("2024-04-30", observed_at),
                        issuer_name="삼성전자", share_class="common", currency="KRW",
                        cash_per_share=361, record_date="2024-03-31",
                        scheduled_payment_date="2024-05-20")
    compared = acquisition.compare_dart_kis_dividend(dart, archive)
    assert compared["state"] == "matched_schedule_fields"
    assert compared["ex_date_confirmed"] is False and compared["actual_payment_confirmed"] is False

    changed_files = dict(files)
    first = json.loads(changed_files["raw/page-001.json"])
    first["output1"][0]["per_sto_divi_amt"] = "362"
    changed_files["raw/page-001.json"] = json.dumps(first).encode()
    changed_manifest = dict(manifest)
    changed_manifest["entries"] = dict(manifest["entries"])
    changed_manifest["entries"]["raw/page-001.json"] = {
        **acquisition._entry(changed_files["raw/page-001.json"], rows=1),
        "page": 1, "tr_cont": "M"}
    conflict_archive = tmp_path / "kis-conflict.zip"
    acquisition.write_archive(conflict_archive, changed_files, changed_manifest)
    assert acquisition.compare_dart_kis_dividend(dart, conflict_archive)["conflicting_fields"] == [
        "cash_per_share"]
    seen.clear()
    with pytest.raises(acquisition.SourceError, match="page cap"):
        acquisition.acquire_kis_schedule("dividend", "005930", "2024-03-01",
                                         "2024-03-31", max_requests=1)

    def repeated(path, tr_id, params, creds, tr_cont):
        body = {"rt_cd": "0", "output1": [{"sht_cd": "005930"}]}
        return json.dumps(body).encode(), body, "M"

    monkeypatch.setattr(acquisition, "_fetch_kis_schedule_page", repeated)
    with pytest.raises(acquisition.SourceError, match="repeated page"):
        acquisition.acquire_kis_schedule("rev-split", "005930", "2024-03-01", "2024-03-31")

    def unauthorized(path, tr_id, params, creds, tr_cont):
        body = {"rt_cd": "1", "msg_cd": "AUTH"}
        return json.dumps(body).encode(), body, ""

    monkeypatch.setattr(acquisition, "_fetch_kis_schedule_page", unauthorized)
    with pytest.raises(acquisition.SourceError, match="response code"):
        acquisition.acquire_kis_schedule("dividend", "005930", "2024-03-01", "2024-03-31")


@pytest.mark.parametrize("kind,expected_path,expected_tr,expected_event", [
    ("bonus-issue", "bonus-issue", "HHKDB669101C0", "rights"),
    ("cap-dcrs", "cap-dcrs", "HHKDB669106C0", "capital_reduction"),
    ("merger-split", "merger-split", "HHKDB669104C0", "merger_exchange"),
    ("paidin-capin", "paidin-capin", "HHKDB669100C0", "rights"),
    ("list-info", "list-info", "HHKDB669107C0", "unsupported"),
])
def test_additional_kis_schedules_remain_partial_research_sources(
        tmp_path, monkeypatch, kind, expected_path, expected_tr, expected_event):
    monkeypatch.setattr(acquisition.config, "kis_credentials", lambda: {"env": "demo"})
    calls = []

    def page(path, tr_id, params, creds, tr_cont):
        calls.append((path, tr_id, params, tr_cont))
        body = {"rt_cd": "0", "output1": []}
        return json.dumps(body).encode(), body, ""

    monkeypatch.setattr(acquisition, "_fetch_kis_schedule_page", page)
    files, manifest = acquisition.acquire_kis_schedule(
        kind, "000660", "2024-06-01", "2024-06-30")
    assert len(calls) == 1
    path, tr_id, params, tr_cont = calls[0]
    assert path.endswith("/" + expected_path) and tr_id == expected_tr and tr_cont == ""
    assert params["SHT_CD"] == "000660"
    assert params["F_DT"] == "20240601" and params["T_DT"] == "20240630"
    assert (params.get("GB1") == "2") == (kind == "paidin-capin")
    assert manifest["source_state"] == "source_empty_not_action_free"
    output = tmp_path / f"{kind}.zip"
    acquisition.write_archive(output, files, manifest)
    source = acquisition.source_window_from_archive(output)
    assert source.event_kinds == frozenset({expected_event})
    assert source.state == "source_empty"
    assert source.date_basis == "unknown" and not source.historical_range_confirmed
    with pytest.raises(acquisition.SourceError, match="unsupported schedule endpoint"):
        acquisition.extract_kis_schedule_facts(output)


def test_kis_split_listing_is_not_assumed_to_be_effective_session(tmp_path):
    raw = json.dumps({"rt_cd": "0", "output1": [{
        "sht_cd": "042700", "isin_name": "한미반도체", "record_date": "20220404",
        "inter_bf_face_amt": "200", "inter_af_face_amt": "100",
        "list_dt": "20220406"}]}).encode()
    manifest = {"schema": "historical-action-source-v1", "source": "KIS KSD schedule",
                "endpoint": "/uapi/domestic-stock/v1/ksdinfo/rev-split",
                "market": "kr", "ticker": "042700", "start": "2022-04-01", "end": "2022-04-30",
                "source_state": "schedule_archived_not_action_complete",
                "entries": {"raw/page-001.json": acquisition._entry(raw, rows=1)},
                "strict_pit_eligible": False, "total_return_coverage": False}
    output = tmp_path / "split.zip"
    acquisition.write_archive(output, {"raw/page-001.json": raw}, manifest)
    fact, = acquisition.extract_kis_schedule_facts(output)
    assert (fact.split_numerator, fact.split_denominator) == (2, 1)
    assert fact.listing_session == "2022-04-06"
    assert fact.effective_session is None
    assert fact.availability.published_on is None


def test_kis_mixed_dividend_is_not_misread_as_cash_only(tmp_path):
    raw = json.dumps({"rt_cd": "0", "output1": [{
        "sht_cd": "005930", "stk_kind": "보통", "record_date": "20240331",
        "per_sto_divi_amt": "361", "divi_pay_dt": "2024/05/20",
        "stk_divi_rate": "1"}]}).encode()
    manifest = {"schema": "historical-action-source-v1", "source": "KIS KSD schedule",
                "endpoint": "/uapi/domestic-stock/v1/ksdinfo/dividend",
                "market": "kr", "ticker": "005930", "start": "2024-03-01", "end": "2024-03-31",
                "source_state": "schedule_archived_not_action_complete",
                "entries": {"raw/page-001.json": acquisition._entry(raw, rows=1)},
                "strict_pit_eligible": False, "total_return_coverage": False}
    output = tmp_path / "mixed.zip"
    acquisition.write_archive(output, {"raw/page-001.json": raw}, manifest)
    with pytest.raises(acquisition.SourceError, match="mixed stock dividend"):
        acquisition.extract_kis_schedule_facts(output)
