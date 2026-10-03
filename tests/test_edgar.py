from signal_desk import store
from signal_desk.ingest import edgar

_INFOTABLE = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><value>6000</value></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><value>2000</value></infoTable>
  <infoTable><nameOfIssuer>COCA COLA CO</nameOfIssuer><value>2000</value></infoTable>
</informationTable>"""


def test_parse_info_table_aggregates_by_issuer():
    rows = edgar._parse_info_table(_INFOTABLE.encode())
    assert len(rows) == 3  # 원자료(합산 전)
    assert {r["name"] for r in rows} == {"APPLE INC", "COCA COLA CO"}


def test_parse_info_table_bad_xml_returns_empty():
    assert edgar._parse_info_table(b"not xml") == []


def test_holdings_13f_aggregates_and_ranks(monkeypatch):
    # 네트워크 없이 파이프라인 검증 — 최신 공시·인덱스·XML을 목킹
    monkeypatch.setattr(edgar, "_latest_13f", lambda cik: ("0001-23-456", "2026-03-31"))
    monkeypatch.setattr(edgar, "_get", lambda url: (
        b'{"directory":{"item":[{"name":"primary_doc.xml"},{"name":"table.xml"}]}}' if url.endswith("index.json")
        else _INFOTABLE.encode()))
    out = edgar.holdings_13f("1067983", top=5)
    assert out["period"] == "2026-03-31" and out["n_holdings"] == 2
    top = out["holdings"][0]
    assert top["name"] == "APPLE INC" and top["pct"] == 80.0  # 8000/10000
    assert out["total_usd"] == 10000.0


def test_holdings_13f_none_when_no_filing(monkeypatch):
    monkeypatch.setattr(edgar, "_latest_13f", lambda cik: None)
    assert edgar.holdings_13f("999") is None


def test_fetch_gurus_skips_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk.ingest import edgar as e
    monkeypatch.setattr(e, "available", lambda: True)
    monkeypatch.setattr(e, "holdings_13f", lambda cik, top=10:
                        {"period": "2026-03-31", "total_usd": 1e9, "n_holdings": 2,
                         "holdings": [{"name": "X", "value_usd": 1e9, "pct": 100.0}]} if cik == "1067983" else None)
    out = store.fetch_gurus()
    assert len(out) == 1 and out[0]["key"] == "berkshire"  # 버크셔만 성공, 나머지 스킵
    assert store.load_gurus() == out


def test_missing_contact_preserves_13f_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk.ingest import edgar as e
    monkeypatch.setattr(e, "available", lambda: False)
    monkeypatch.setattr(e, "holdings_13f", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")))
    old = [{"key": "berkshire", "period": "2025-12-31", "holdings": []}]
    store._write_json(store.GURUS_FILE, old)
    assert store.fetch_gurus() == old
    assert store.load_gurus() == old


def test_sec_request_requires_real_contact_and_approved_host(monkeypatch):
    from signal_desk.ingest import edgar as e
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(e.urllib.request, "build_opener", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    assert e._get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json") is None
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "admin@signal-desk.local")
    assert e._get("https://www.sec.gov/files/company_tickers.json") is None
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "ops@example.org")
    assert e._get("https://www.sec.gov/files/company_tickers.json") is None
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "ops@signal-desk.org")
    assert e._get("https://evil.example/company_tickers.json") is None


def test_missing_contact_does_not_poison_cik_mapping_cache(monkeypatch):
    from signal_desk.ingest import edgar as e
    monkeypatch.setattr(e, "_cik_map", None)
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    assert e._ticker_cik_map() == {}
    assert e._cik_map is None
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "ops@signal-desk.org")
    monkeypatch.setattr(e, "_get", lambda _url: b'{"0":{"ticker":"AAPL","cik_str":320193}}')
    assert e._ticker_cik_map()["AAPL"] == "0000320193"


def test_sec_request_uses_configured_identity_and_bounded_response(monkeypatch):
    from signal_desk.ingest import edgar as e
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "ops@signal-desk.org")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, limit):
            captured["limit"] = limit
            return b"{}"

    class Opener:
        def open(self, req, timeout):
            captured["agent"] = req.get_header("User-agent")
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(e.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(e.time, "sleep", lambda *_: None)
    assert e._get("https://www.sec.gov/files/company_tickers.json") == b"{}"
    assert "ops@signal-desk.org" in captured["agent"]
    assert captured["limit"] == e._MAX_RESPONSE_BYTES + 1
    assert captured["timeout"] == e._TIMEOUT
