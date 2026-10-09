import json
from zipfile import ZipFile

import pytest

from scripts.measure import acquire_historical_sources as acquisition


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
