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
