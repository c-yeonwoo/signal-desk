"""Audit a free vendor sample; never fetch paid data or certify action coverage."""

import argparse
from collections import Counter
import csv
import datetime as dt
import io
import json
from pathlib import Path

from scripts.measure.acquire_historical_sources import write_archive
from scripts.measure.kr_action_source_audit import Collector, VENDOR_DOCUMENTS
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest
from scripts.measure.kr_net_alpha_v2 import save, source_storage_bytes

SAMPLE = "https://sample-pcaps-dl.databento.com/reference/databento-corporate-actions-global-20200825.csv"
UNIVERSE_SHA = "98060cf7f0ee3a881cf27680a4147941dafacdeeacd28cb0aa33c6e840209ccb"
FIELDS = ("event_date", "event_unique_id", "event_id", "event_action", "event",
    "ts_record", "ts_created", "effective_date", "ex_date", "record_date", "payment_date",
    "listing_country", "exchange", "security_type", "local_code", "isin")


def inspect_sample(raw, roster_isins):
    """Strings stay strings: KR ISINs overseas are not domestic listings."""
    if len(raw) > 5_000_000:
        raise ValueError("sample byte limit exceeded")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    names = reader.fieldnames or []
    if len(names) != len(set(names)) or not set(FIELDS).issubset(names):
        raise ValueError("sample columns missing or duplicated")
    rows = list(reader)
    for row in rows:
        if None in row or any(v is None for v in row.values()) or not row["event_unique_id"]:
            raise ValueError("sample row shape or identifier invalid")
        for key in ("ts_record", "ts_created"):
            stamp = dt.datetime.fromisoformat(row[key])
            if stamp.utcoffset() != dt.timedelta(0):
                raise ValueError("sample record clock must be UTC")
        if dt.date.fromisoformat(row["event_date"]).isoformat() != row["event_date"]:
            raise ValueError("sample event date invalid")
    domestic = [r for r in rows if r["listing_country"] == "KR"]
    by_id = Counter(r["event_unique_id"] for r in rows)
    dates = sorted({r["event_date"] for r in rows})
    return {"total_rows":len(rows), "event_dates":dates,
        "domestic_listing_rows":len(domestic),
        "kr_isin_on_non_kr_listing_rows":sum(r["listing_country"] != "KR" and
            r["isin"].startswith("KR") for r in rows),
        "domestic_exchanges":dict(sorted(Counter(r["exchange"] for r in domestic).items())),
        "domestic_security_types":dict(sorted(Counter(r["security_type"] for r in domestic).items())),
        "domestic_event_types":dict(sorted(Counter(r["event"] for r in domestic).items())),
        "domestic_kse_equity_rows":sum(r["exchange"] == "KRKSE" and r["security_type"] == "EQS"
            for r in domestic),
        "frozen_roster_isin_matching_rows":sum(r["isin"] in roster_isins for r in rows),
        "domestic_frozen_roster_isin_matching_rows":sum(r["isin"] in roster_isins for r in domestic),
        "event_unique_ids":len(by_id),
        "ids_with_multiple_rows":sum(count > 1 for count in by_id.values()),
        "domestic_records_changed_after_event_date":sum(
            dt.datetime.fromisoformat(r["ts_record"]).date().isoformat() > r["event_date"]
            for r in domestic),
        "domestic_rows":[{k:r[k] for k in FIELDS} for r in domestic],
        "full_revision_chain_verified":False, "scope_certificate_eligible":False,
        "source_publication_clock_verified":False, "payment_completed_verified":False,
        "fit_executed":False, "backtest_executed":False, "adoption":"not_evaluable_data_gate"}


def run(output, report_path):
    output, report_path = Path(output).resolve(), Path(report_path).resolve()
    if output == report_path or any(not p.is_relative_to((ROOT/"data/research").resolve()) or p.exists()
            for p in (output, report_path)):
        raise ValueError("requires distinct new local research outputs")
    roster_raw = (DEST/"universes.json").read_bytes()
    if digest(roster_raw) != UNIVERSE_SHA:
        raise ValueError("frozen roster differs")
    roster = json.loads(roster_raw)
    isins = {s["isin"] for day in roster["dates"] for s in day["securities"]}
    c = Collector()
    before = len(c.journal)
    files, observations = {}, []
    for index, url in enumerate(sorted(VENDOR_DOCUMENTS)):
        raw, meta = c.get(url)
        name = f"raw/{index:02d}" + (".csv" if url == SAMPLE else ".html")
        files[name] = raw
        observations.append({"url":url, "raw_member":name, "bytes":len(raw),
            "sha256":digest(raw), "observed_at":meta["observed_at"],
            "source_response_path":meta["response_path"], "is_economic_sample":url == SAMPLE,
            "is_coverage_certificate":False})
        if url == SAMPLE:
            sample = inspect_sample(raw, isins)
    result = {"schema":"kr-net-alpha-vendor-sample-review-v1", "candidate_id":c.spec["candidate_id"],
        "contract_hashes":c.spec["contract_hashes"], "roster_sha256":UNIVERSE_SHA,
        "observations":observations, "sample":sample, "new_requests":len(c.journal)-before,
        "cumulative_action_attempts":7+len(c.journal), "paid_requests":0, "new_contracts":0,
        "purchase_authorized":False, "scope_certificates_added":0, "integrated_progress":40,
        "implementation_sha256":{p:digest((ROOT/p).read_bytes()) for p in (
            "scripts/measure/kr_action_source_audit.py", "scripts/measure/kr_net_alpha_vendor_sample.py")}}
    files["review.json"] = json.dumps(result,ensure_ascii=False,sort_keys=True).encode()
    files["journal.json"] = json.dumps(c.journal,ensure_ascii=False,sort_keys=True).encode()
    for p in result["implementation_sha256"]:
        files["implementation/"+Path(p).name] = (ROOT/p).read_bytes()
    if source_storage_bytes()+sum(map(len,files.values())) > c.spec["max_new_archive_bytes"]:
        raise ValueError("vendor archive exceeds remaining source byte budget")
    write_archive(output,files,{"schema":"kr-net-alpha-vendor-originals-v1",
        "entries":{n:{"sha256":digest(raw),"bytes":len(raw)} for n,raw in files.items()},
        "economic_scope_certified":False})
    result.update(archive=str(output.relative_to(ROOT)), archive_sha256=digest(output.read_bytes()))
    save(report_path,result)
    print(json.dumps({k:result[k] for k in ("new_requests","cumulative_action_attempts",
        "paid_requests","scope_certificates_added","archive_sha256")},ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--report",type=Path,required=True)
    args = parser.parse_args()
    run(args.output,args.report)
