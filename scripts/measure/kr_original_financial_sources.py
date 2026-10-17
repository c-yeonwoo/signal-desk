"""Restore the frozen 20-company FY2025 receipt chain, research only."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import ROOT, SourceError, _entry, acquire_dart, write_archive
from scripts.measure.kr_financial_input_sources import load as load_financials
from signal_desk import config, market_clock
from signal_desk.signals.research_financial_inputs import original_annual

FINANCIAL = ROOT / "data/research/kr-receipt-financial-inputs-2026-10-17-v1.zip"
FINANCIAL_SHA = "bdab1956d2f1055a31a012fc8c3e5ecbb442de21c95c574beeb37aaf7c7d085c"
MARCH = ROOT / "data/research/dart-annual-2026-03-v1.zip"
MARCH_SHA = "ac8852766ddd0f419b2d39a33babbaa999cd68e4012b1bbc115c77f029838423"


def _filings(files, entries):
    rows, counts, pages = [], set(), set()
    for member, entry in entries.items():
        raw = files[member]
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("filing-list source hash differs")
        body = json.loads(raw)
        if body.get("status") != "000":
            raise ValueError("filing-list response unavailable")
        counts.add((int(body["total_count"]), int(body["total_page"])))
        page = int(body["page_no"])
        if page in pages:
            raise ValueError("duplicated filing-list page")
        pages.add(page)
        rows.extend(body["list"])
    if len(counts) != 1:
        raise ValueError("filing-list totals differ")
    total, n_pages = next(iter(counts))
    if (len(rows) != total or pages != set(range(1, n_pages + 1))
            or len({r["rcept_no"] for r in rows}) != total):
        raise ValueError("incomplete filing-list pages")
    return rows


def frozen_targets():
    if hashlib.sha256(FINANCIAL.read_bytes()).hexdigest() != FINANCIAL_SHA:
        raise ValueError("financial source hash differs")
    if hashlib.sha256(MARCH.read_bytes()).hexdigest() != MARCH_SHA:
        raise ValueError("March listing hash differs")
    financials = load_financials(FINANCIAL)
    with ZipFile(MARCH) as archive:
        m = json.loads(archive.read("manifest.json"))
        files = {p: archive.read(p) for p in m["entries"]}
    march = _filings(files, m["entries"])
    targets = {r["ticker"]: r["corp_code"] for r in financials["records"]
               if r["year"] == 2025 and r.get("filing_date", "") >= "2026-06-01"
               and any(f["stock_code"] == r["ticker"] and f["corp_code"] == r["corp_code"]
                       and "사업보고서" in f["report_nm"] and "(2025.12)" in f["report_nm"] for f in march)}
    if len(targets) != 20:
        raise ValueError("frozen 20-company recovery scope differs")
    return targets, march


def _normalize(raw, filing):
    ticker, corp, receipt = filing["stock_code"], filing["corp_code"], filing["rcept_no"]
    day = filing["rcept_dt"]
    if (len(receipt) != 14 or not receipt.isdigit() or receipt[:8] != day
            or not "20260301" <= day <= "20260731"):
        raise ValueError("original receipt date outside recovery scope")
    filed = dt.date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:8]}")
    after = filed + dt.timedelta(days=1)
    while not market_clock.is_session("kr", after):
        after += dt.timedelta(days=1)
    try:
        return original_annual(raw, ticker=ticker, corp_code=corp, year=2025, receipt=receipt)
    except Exception as exc:
        # A missing/custom/failed correction is a blocking version, not permission
        # to continue using the earlier successful financial statement.
        try:
            status = ET.fromstring(raw).findtext("status") if raw.startswith(b"<?xml") else None
        except ET.ParseError:
            status = None
        return {"ticker": ticker, "corp_code": corp, "year": 2025, "receipt": receipt,
                "filing_date": filed.isoformat(), "usable_from_session_assumption": after.isoformat(),
                "status": "original_unavailable_or_ambiguous", "failure_type": type(exc).__name__,
                "dart_response_status": status,
                "strict_pit_eligible": False, "original_document_matched": False}


def acquire():
    targets, march = frozen_targets()
    files, entries, lists = {}, {}, []
    # Freeze selection using existing metadata, before acquiring any new facts.
    target_sha = hashlib.sha256(json.dumps(targets, sort_keys=True).encode()).hexdigest()
    print(json.dumps({"frozen_companies": len(targets), "target_sha256": target_sha}), flush=True)
    all_rows = list(march)
    for start, end in (("2026-04-01", "2026-05-31"), ("2026-06-01", "2026-07-31")):
        raw_files, listing = acquire_dart(start, end, "A001")
        all_rows.extend(_filings(raw_files, listing["entries"]))
        prefix = f"lists/{start}/"
        for p, raw in raw_files.items():
            files[prefix + p] = raw
            entries[prefix + p] = listing["entries"][p]
        lists.append(listing)
    queue = [r for r in all_rows if r["stock_code"] in targets
             and r["corp_code"] == targets[r["stock_code"]]
             and "사업보고서" in r["report_nm"] and "(2025.12)" in r["report_nm"]]
    queue.sort(key=lambda r: (r["rcept_dt"], r["rcept_no"]))
    if not 26 <= len(queue) <= 80 or len({r["rcept_no"] for r in queue}) != len(queue):
        raise ValueError("recovery receipt queue outside request cap or duplicated")
    current = load_financials(FINANCIAL)
    receipts = {r["rcept_no"] for r in queue}
    for r in current["records"]:
        if r["ticker"] in targets and r["year"] == 2025 and r.get("filing_date", "9999") <= "2026-07-31":
            if r.get("receipt") not in receipts:
                raise ValueError("known annual version missing from listing chain")
    attempts = 0
    records = []
    for filing in queue:
        receipt = filing["rcept_no"]
        url = "https://opendart.fss.or.kr/api/fnlttXbrl.xml?" + urllib.parse.urlencode(
            {"crtfc_key": config.dart_key(), "rcept_no": receipt, "reprt_code": "11011"})
        raw, failure = b"", None
        for attempt in range(3):
            attempts += 1
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise SourceError("original compressed source exceeds cap")
                failure = None
                break
            except Exception as exc:
                failure = type(exc).__name__  # never expose URL containing key
                if attempt < 2:
                    time.sleep(1)
        member = f"originals/{receipt}.zip"
        files[member] = raw
        entries[member] = {**_entry(raw, rows=1), "filing": filing,
                           "request_failure_type": failure}
        record = _normalize(b"" if failure else raw, filing)
        records.append(record)
        print(json.dumps({"ticker": record["ticker"], "receipt": receipt, "status": record["status"]}), flush=True)
        time.sleep(.5)
    manifest = {"schema": "kr-original-financial-versions-v1", "financial_archive_sha256": FINANCIAL_SHA,
                "march_archive_sha256": MARCH_SHA, "target_sha256": target_sha, "targets": targets,
                "start": "2026-03-01", "end": "2026-07-31", "listings": lists, "queue": queue,
                "entries": entries, "records": records, "xbrl_http_attempts": attempts,
                "logical_requests": sum(m["request_count"] for m in lists) + len(queue),
                "strict_pit_eligible": False, "live_order_eligible": False,
                "limits": ["Only the frozen 20 FY2025 companies; not complete historical inputs",
                           "Missing standard revenue facts stay absent; custom insurance/bank accounts are not substituted",
                           "Date-only publication; use from next regular session, not intraday",
                           "Latest available dated version including failed amendments blocks older facts",
                           "No quarter updates, original provider cache or full corporate-action certificate"]}
    return files, manifest


def load(path: Path):
    targets, march = frozen_targets()
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if (manifest.get("schema") != "kr-original-financial-versions-v1"
                or manifest["financial_archive_sha256"] != FINANCIAL_SHA
                or manifest["march_archive_sha256"] != MARCH_SHA or manifest["targets"] != targets
                or (manifest["start"], manifest["end"]) != ("2026-03-01", "2026-07-31")):
            raise ValueError("recovery archive scope mismatch")
        if [(m["start"], m["end"]) for m in manifest["listings"]] != [
                ("2026-04-01", "2026-05-31"), ("2026-06-01", "2026-07-31")]:
            raise ValueError("recovery listing ranges incomplete")
        for listing in manifest["listings"]:
            q = listing["query"]
            if (q.get("last_reprt_at"), q.get("corp_cls"), q.get("pblntf_detail_ty")) != ("N", "Y", "A001"):
                raise ValueError("recovery correction listing query differs")
        raw_files = {p: archive.read(p) for p in manifest["entries"]}
        for p, entry in manifest["entries"].items():
            if hashlib.sha256(raw_files[p]).hexdigest() != entry["sha256"]:
                raise ValueError("recovery source digest differs")
        rows = list(march)
        for listing in manifest["listings"]:
            prefix = f"lists/{listing['start']}/"
            rows.extend(_filings({p: raw_files[prefix + p] for p in listing["entries"]}, listing["entries"]))
        queue = sorted((r for r in rows if r["stock_code"] in targets and r["corp_code"] == targets[r["stock_code"]]
                        and "사업보고서" in r["report_nm"] and "(2025.12)" in r["report_nm"]),
                       key=lambda r: (r["rcept_dt"], r["rcept_no"]))
        if queue != manifest["queue"]:
            raise ValueError("recovery version queue differs")
        records = []
        for r in queue:
            member = f"originals/{r['rcept_no']}.zip"
            entry = manifest["entries"][member]
            records.append(_normalize(b"" if entry.get("request_failure_type") else raw_files[member], r))
        return {**manifest, "records": records, "tickers": sorted(targets),
                "normalizer_version": 2,
                "acquisition_status_counts": dict(Counter(r["status"] for r in manifest["records"])),
                "status_counts": dict(Counter(r["status"] for r in records)),
                "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite original evidence")
    config.load_env()
    files, manifest = acquire()
    write_archive(args.output, files, manifest)
    checked = load(args.output)
    print(json.dumps({"output": str(args.output), "sha256": checked["archive_sha256"],
                      "status_counts": checked["status_counts"], "requests": checked["logical_requests"]}))


if __name__ == "__main__":
    main()
