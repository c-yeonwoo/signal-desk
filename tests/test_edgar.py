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
    monkeypatch.setattr(e, "holdings_13f", lambda cik, top=10:
                        {"period": "2026-03-31", "total_usd": 1e9, "n_holdings": 2,
                         "holdings": [{"name": "X", "value_usd": 1e9, "pct": 100.0}]} if cik == "1067983" else None)
    out = store.fetch_gurus()
    assert len(out) == 1 and out[0]["key"] == "berkshire"  # 버크셔만 성공, 나머지 스킵
    assert store.load_gurus() == out
