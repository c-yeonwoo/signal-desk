"""Vendor samples are not action-scope or profitability evidence."""

import csv
import io

import pytest

from scripts.measure import kr_action_source_audit as transport
from scripts.measure.kr_net_alpha_vendor_sample import FIELDS, SAMPLE, inspect_sample
from test_kr_action_source_audit import collector


def sample(*changes):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream,fieldnames=FIELDS)
    writer.writeheader()
    base = {k:"" for k in FIELDS}
    base.update(event_date="2020-08-25",event_unique_id="one",event_id="event",event="SHOCH",
        ts_record="2020-08-26 17:00:00+00:00",ts_created="2024-08-17 07:00:00+00:00",
        listing_country="KR",exchange="KRKSE",security_type="EQS",isin="KR7001780006",local_code="001780")
    for change in changes:
        writer.writerow({**base,**change})
    return stream.getvalue().encode()


def test_exact_isin_and_listing_scope_no_zero_padding_or_foreign_rebinding():
    data = inspect_sample(sample({}, {"event_unique_id":"two", "listing_country":"US","exchange":"USOTC"},
        {"event_unique_id":"three","isin":"KR7001781004","local_code":"00178"}), {"KR7001780006"})
    assert data["domestic_listing_rows"] == 2
    assert data["kr_isin_on_non_kr_listing_rows"] == 1
    assert data["frozen_roster_isin_matching_rows"] == 2  # Includes the foreign listing, explicitly not domestic coverage.
    assert data["domestic_frozen_roster_isin_matching_rows"] == 1
    assert data["domestic_records_changed_after_event_date"] == 2
    assert not data["scope_certificate_eligible"] and not data["source_publication_clock_verified"]
    assert not data["payment_completed_verified"] and not data["fit_executed"]


def test_more_than_one_version_or_an_empty_sample_still_does_not_certify_scope():
    data = inspect_sample(sample({}, {"event_action":"U","ts_record":"2020-08-27 17:00:00+00:00"}),set())
    assert data["ids_with_multiple_rows"] == 1
    assert not data["full_revision_chain_verified"]
    empty = inspect_sample(sample(),set())
    assert empty["total_rows"] == 0 and not empty["scope_certificate_eligible"]


@pytest.mark.parametrize("raw", [b'<html>HTTP 200 error</html>', sample({}).replace(b'event_id,',b'isin,'),
    sample({"ts_record":"2020-08-26 17:00:00"}),sample({"event_unique_id":""}),
    sample({"event_date":"2020-02-30"})])
def test_malformed_sample_or_ambiguous_record_clock_rejected(raw):
    with pytest.raises(ValueError):
        inspect_sample(raw,set())


def test_only_exact_public_vendor_gets_are_authorized(tmp_path, monkeypatch):
    c = collector(tmp_path, monkeypatch)

    class Response(io.BytesIO):
        code, headers = 200, {}

    class Opener:
        def open(self, request, timeout):
            assert c.journal[-1]["state"] == "reserved"
            assert request.full_url == SAMPLE and request.data is None
            return Response(sample({}))

    monkeypatch.setattr(transport.urllib.request,"build_opener",lambda *args:Opener())
    for url, kwargs in ((SAMPLE+"?api_key=secret",{}),
        ("https://databento.com/login",{}),
        ("https://hist.databento.com/v0/corporate_actions.get_range",{}),
        (SAMPLE,{"data":b'purchase=yes'}),(SAMPLE,{"_service_key":"SYNTHETIC"})):
        with pytest.raises(ValueError,match="outside read-only"):
            c.get(url,**kwargs)
    assert c.journal == []
    c.get(SAMPLE)
    c.get(SAMPLE)
    assert len(c.journal) == 1 and not c.journal[0].get("economic_scope_certified", False)
