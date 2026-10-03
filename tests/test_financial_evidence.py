import json
import sqlite3

import pytest
from typer.testing import CliRunner

from signal_desk.ingest import financial_evidence as e


EARLY = "2026-10-03T00:00:00+00:00"
LATE = "2026-10-04T00:00:00+00:00"
SEC = e.Target("sec", "0000320193")
DART = e.Target("dart", "00126380", "2026", "11012")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(e, "_now", lambda: EARLY)


def sec_body():
    return {"cik": 320193, "facts": {"us-gaap": {"Revenues": {
        "label": "Revenue", "units": {"USD": [
            {"val": 100, "start": "2026-04-01", "end": "2026-06-30", "filed": "2026-07-30",
             "form": "10-Q", "accn": "0000320193-26-000001", "fp": "Q2", "fy": 2026},
            {"val": 190, "start": "2026-01-01", "end": "2026-06-30", "filed": "2026-07-30",
             "form": "10-Q", "accn": "0000320193-26-000001", "fp": "Q2", "fy": 2026},
            {"val": 99, "start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-30",
             "form": "10-Q/A", "accn": "0000320193-26-000002", "fp": "Q2", "fy": 2026},
        ]}}}}}


def dart_body():
    return {"status": "000", "list": [{
        "corp_code": "00126380", "bsns_year": "2026", "reprt_code": "11012",
        "rcept_no": "20260814000001", "sj_div": "IS", "account_id": "ifrs-full_Revenue",
        "account_nm": "매출액", "thstrm_nm": "제 57 기 반기", "currency": "KRW",
        "thstrm_amount": "1,000", "thstrm_add_amount": "1,900"}]}


def encoded(body):
    return json.dumps(body).encode()


def test_sec_periods_revisions_not_collapsed():
    status, facts = e.normalize(SEC, sec_body())
    assert status == "ok"
    assert [f["value"] for f in facts] == ["100", "190", "99"]
    assert [f["period_kind"] for f in facts] == ["quarter_length", "reported_duration", "quarter_length"]
    assert all(f["source_published_at"] is None for f in facts)
    assert facts[2]["accession"] != facts[0]["accession"]


def test_dart_quarter_and_ytd_no_calendar_invention():
    _, facts = e.normalize(DART, dart_body())
    assert [f["value"] for f in facts] == ["1000", "1900"]
    assert [f["period_kind"] for f in facts] == ["quarter", "year_to_date"]
    assert all(f["period_end"] is None and f["filing_date"] is None for f in facts)
    assert all(f["basis"] == "CFS" and f["unit"] == "KRW" for f in facts)


def test_dart_cashflow_not_assumed_quarter():
    body = dart_body()
    body["list"][0]["sj_div"] = "CF"
    assert e.normalize(DART, body)[1][0]["period_kind"] == "reported_duration"
    body["list"][0]["sj_div"] = "BS"
    facts = e.normalize(DART, body)[1]
    assert len(facts) == 1 and facts[0]["period_kind"] == "instant"


@pytest.mark.parametrize("value", [None, "-", "", "NaN", "Infinity", True])
def test_missing_invalid_never_zero(value):
    assert e._number(value) is None


def test_exact_zero_negative_large_number():
    assert e._number("0") == "0"
    assert e._number("-1,234") == "-1234"
    assert e._number("99999999999999999999") == "99999999999999999999"


@pytest.mark.parametrize("field,value", [("corp_code", "99999999"), ("fs_div", "OFS"),
                                         ("bsns_year", "2025"), ("reprt_code", "11013")])
def test_dart_identity_and_scope_fail_closed(field, value):
    body = dart_body()
    body["list"][0][field] = value
    with pytest.raises(ValueError):
        e.normalize(DART, body)


def test_archive_asof_does_not_backdate_and_retains_revisions(tmp_path):
    path = tmp_path / "financial.db"
    first = e.archive(path, SEC, encoded(sec_body()), observed_at=EARLY)
    assert e.latest(path, SEC, as_of="2026-10-02T23:59:59Z") is None
    body = sec_body()
    body["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["val"] = 101
    second = e.archive(path, SEC, encoded(body), observed_at=LATE)
    assert first["id"] != second["id"]
    assert e.latest(path, SEC, as_of=EARLY)["facts"][0]["value"] == "100"
    assert e.latest(path, SEC, as_of=LATE)["facts"][0]["value"] == "101"
    assert not e.latest(path, SEC, as_of=LATE)["strict_pit_eligible"]
    assert e.archive(path, SEC, encoded(sec_body()), observed_at=EARLY) == first


def test_no_data_is_retained_not_old_data_or_zero(tmp_path):
    path = tmp_path / "financial.db"
    e.archive(path, DART, encoded(dart_body()), observed_at=EARLY)
    result = e.archive(path, DART, encoded({"status": "013"}), observed_at=LATE)
    assert result["status"] == "no_data"
    assert e.latest(path, DART, as_of=LATE)["facts"] == []
    with pytest.raises(ValueError):
        e.archive(path, DART, encoded({"status": "020"}), observed_at=LATE)


@pytest.mark.parametrize("column,value", [("raw", b"{}"), ("envelope", b"{}"),
                                         ("observed_at", "2026-10-02T00:00:00+00:00")])
def test_archive_tampering_detected(tmp_path, column, value):
    path = tmp_path / "financial.db"
    e.archive(path, SEC, encoded(sec_body()), observed_at=EARLY)
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE financial_observations SET {column}=?", (value,))
    with pytest.raises(ValueError):
        e.latest(path, SEC, as_of=LATE)


def test_timezones_and_invalid_targets(tmp_path):
    for args in [("sec", "../etc"), ("dart", "00126380"), ("other", "abc")]:
        with pytest.raises(ValueError):
            e.Target(*args)
    with pytest.raises(ValueError):
        e.archive(tmp_path / "e.db", SEC, encoded(sec_body()), observed_at="2026-10-03")


def test_transport_failure_secret_not_logged_or_persisted(tmp_path, monkeypatch, caplog):
    class BadOpener:
        def open(self, request, **kwargs):
            assert "crtfc_key=secret" in request.full_url
            raise OSError(request.full_url)
    monkeypatch.setattr(e.urllib.request, "build_opener", lambda *args: BadOpener())
    path = tmp_path / "financial.db"
    assert e.collect(path, DART, dart_key="secret")["status"] == "collection_failed"
    assert "secret" not in caplog.text
    assert not path.exists()


def test_collect_once_with_response_bound(tmp_path, monkeypatch):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            assert size == e.MAX_BYTES + 1
            return encoded(sec_body())
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            assert "contact@example.com" in request.get_header("User-agent")
            return Response()
    monkeypatch.setattr(e.urllib.request, "build_opener", lambda *args: Opener())
    result = e.collect(tmp_path / "e.db", SEC, sec_contact="contact@example.com")
    assert result["status"] == "ok" and len(calls) == 1


def test_missing_credentials_no_network(tmp_path):
    assert e.collect(tmp_path / "e.db", SEC)["status"] == "missing_contact"
    assert e.collect(tmp_path / "e.db", DART)["status"] == "missing_credentials"


def test_parse_completion_prevents_early_visibility(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "_now", lambda: LATE)
    path = tmp_path / "e.db"
    e.archive(path, SEC, encoded(sec_body()), observed_at=EARLY)
    assert e.latest(path, SEC, as_of=EARLY) is None
    assert e.latest(path, SEC, as_of=LATE)["available_at"] == LATE


def test_sec_missing_start_not_mistaken_for_balance_sheet():
    body = sec_body()
    del body["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["start"]
    assert e.normalize(SEC, body)[1][0]["period_kind"] == "unknown"


def test_response_size_limit_leaves_no_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="large"):
        e.archive(tmp_path / "e.db", SEC, b"{}", observed_at=EARLY)
    assert not (tmp_path / "e.db").exists()


def test_sec_currency_taxonomy_and_reported_intervals_preserved():
    body = sec_body()
    body["facts"]["ifrs-full"] = {"Revenue": {"units": {"EUR": [{
        "val": 123, "start": "2025-10-01", "end": "2026-09-30", "filed": "2026-10-02",
        "form": "20-F", "accn": "0000320193-26-000003", "fp": "FY"}]}}}
    facts = e.normalize(SEC, body)[1]
    assert facts[-1]["unit"] == "EUR"
    assert facts[-1]["concept"] == "ifrs-full:Revenue"
    assert facts[-1]["period_start"] == "2025-10-01"
    assert facts[-1]["period_kind"] == "reported_duration"  # never manufacture Q4


def test_sec_wrong_issuer_and_invalid_period_fail_closed():
    body = sec_body()
    body["cik"] = 123
    with pytest.raises(ValueError):
        e.normalize(SEC, body)
    body = sec_body()
    body["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["start"] = "2027-01-01"
    with pytest.raises(ValueError):
        e.normalize(SEC, body)


def test_no_supported_facts_not_zero_financials():
    assert e.normalize(SEC, {"cik": 320193, "facts": {}}) == ("no_supported_facts", [])


def test_cli_rejects_operational_database(tmp_path):
    from signal_desk.cli import app
    result = CliRunner().invoke(app, ["financial-evidence", "--source", "sec", "--issuer", SEC.issuer,
                                      "--archive", str(tmp_path / "app.db")])
    assert result.exit_code != 0 and not (tmp_path / "app.db").exists()


def test_cli_read_only_does_not_fetch(tmp_path, monkeypatch):
    from signal_desk.cli import app
    monkeypatch.setattr(e, "collect", lambda *args, **kwargs: pytest.fail("must not collect"))
    result = CliRunner().invoke(app, ["financial-evidence", "--source", "sec", "--issuer", SEC.issuer,
                                      "--archive", str(tmp_path / "e.db"), "--as-of", EARLY])
    assert result.exit_code == 0 and "not_observed" in result.stdout
