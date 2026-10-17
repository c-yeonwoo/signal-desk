"""Bounded DART annual acquisition for the frozen 256-name domestic census."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import ROOT, SourceError, _entry, _fetch_json, write_archive
from signal_desk import config
from signal_desk.signals.research_financial_inputs import normalize_annual

SCOPE = ROOT / "data/research/kr-selection-holding-audit-2026-10-17-v3.json"
SCOPE_SHA = "8b98fbf085b9b610ffe23afaf5ba84faa0da1d6de223ec42021c8eba923705f6"
YEARS = (2022, 2023, 2024, 2025)  # FY2022 is needed for prior-year quality in 2024.


def acquire() -> tuple[dict, dict]:
    frozen = SCOPE.read_bytes()
    if hashlib.sha256(frozen).hexdigest() != SCOPE_SHA:
        raise ValueError("domestic census hash differs")
    scope = json.loads(frozen)
    tickers = sorted({r["ticker"] for w in scope["windows"].values() for r in w["selection"]["rows"]})
    key = config.dart_key()
    if not key:
        raise SourceError("DART key unavailable")
    url = "https://opendart.fss.or.kr/api/corpCode.xml?" + urllib.parse.urlencode({"crtfc_key": key})
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read()
        with ZipFile(io.BytesIO(raw)) as z:
            tree = ET.fromstring(z.read("CORPCODE.xml"))
    except Exception as exc:
        raise SourceError(f"DART company map: {type(exc).__name__}") from None
    mapping = {}
    for item in tree.iter("list"):
        ticker = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if ticker in tickers:
            if ticker in mapping and mapping[ticker] != corp:
                raise ValueError("ambiguous company mapping")
            mapping[ticker] = corp
    files = {"raw/corpCode.zip": raw}
    entries = {"raw/corpCode.zip": _entry(raw, rows=len(mapping))}
    batches = [sorted(mapping)[i:i + 100] for i in range(0, len(mapping), 100)]
    if len(tickers) != 256 or len(batches) > 3:
        raise ValueError("unexpected domestic request scope")
    records, failures = [], []
    for year in YEARS:
        for index, batch in enumerate(batches):
            params = {"corp_code": ",".join(mapping[t] for t in batch), "bsns_year": str(year),
                      "reprt_code": "11011", "crtfc_key": key}
            raw, body = _fetch_json("https://opendart.fss.or.kr/api/fnlttMultiAcnt.json", params, {}, "DART annual")
            if body.get("status") not in ("000", "013"):
                raise SourceError(f"DART annual status {body.get('status')}")
            rows = body.get("list", [])
            if not isinstance(rows, list) or (body["status"] == "000" and not rows):
                raise SourceError("DART annual malformed account rows")
            if any(not isinstance(r, dict) or r.get("stock_code") not in batch for r in rows):
                raise SourceError("DART annual response outside requested companies")
            name = f"raw/{year}-{index:02d}.json"
            files[name] = raw
            entries[name] = {**_entry(raw, rows=len(rows)), "year": year, "tickers": batch,
                             "status": body["status"]}
            for ticker in batch:
                subset = [r for r in rows if r["stock_code"] == ticker]
                try:
                    record = normalize_annual(subset, ticker=ticker, corp_code=mapping[ticker], year=year)
                except ValueError as exc:
                    record = {"ticker": ticker, "year": year, "status": "ambiguous_accounts", "reason": str(exc)}
                    failures.append({"ticker": ticker, "year": year, "reason": str(exc)})
                records.append({**record, "raw_member": name, "raw_sha256": entries[name]["sha256"]})
            time.sleep(.5)
    manifest = {"schema": "kr-receipt-financial-inputs-v1", "source": "OpenDART official API",
                "scope_sha256": SCOPE_SHA, "tickers": tickers, "years": YEARS,
                "mapping_observed_now": True, "unmapped_tickers": sorted(set(tickers) - set(mapping)),
                "logical_requests": len(entries), "entries": entries,
                "records": records, "normalization_failures": failures,
                "status_counts": dict(Counter(r["status"] for r in records)),
                "strict_pit_eligible": False, "live_order_eligible": False,
                "limits": ["Current API may return later corrected filings; receipt dates are guarded per decision",
                           "Original XBRL matching and correction-chain completeness are not certified",
                           "Annual December-year sensitivity, not the historical operating input/cache",
                           "Flow, short, qualitative and corporate-action coverage remain separate"]}
    return files, manifest


def load(path: Path) -> dict:
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != "kr-receipt-financial-inputs-v1" or manifest.get("scope_sha256") != SCOPE_SHA:
            raise ValueError("financial source scope mismatch")
        for member, entry in manifest["entries"].items():
            if hashlib.sha256(archive.read(member)).hexdigest() != entry["sha256"]:
                raise ValueError("financial raw digest mismatch")
        with ZipFile(io.BytesIO(archive.read("raw/corpCode.zip"))) as companies:
            tree = ET.fromstring(companies.read("CORPCODE.xml"))
        mapping = {(item.findtext("stock_code") or "").strip(): (item.findtext("corp_code") or "").strip()
                   for item in tree.iter("list") if (item.findtext("stock_code") or "").strip()}
        # Recompute metrics from raw bytes, not an editable derived manifest.
        records = []
        for member, entry in manifest["entries"].items():
            if not member.endswith(".json"):
                continue
            rows = json.loads(archive.read(member)).get("list", [])
            for ticker in entry["tickers"]:
                try:
                    record = normalize_annual([r for r in rows if r["stock_code"] == ticker],
                                              ticker=ticker, corp_code=mapping[ticker],
                                              year=entry["year"])
                except ValueError as exc:
                    record = {"ticker": ticker, "year": entry["year"],
                              "status": "ambiguous_accounts", "reason": str(exc)}
                records.append({**record, "raw_member": member, "raw_sha256": entry["sha256"]})
        return {**manifest, "records": records, "acquisition_status_counts": manifest["status_counts"],
                "status_counts": dict(Counter(r["status"] for r in records)),
                "normalizer_version": 2, "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def compare_original_samples(path: Path, financials: dict) -> list[dict]:
    """Compare selected original XBRL totals; never certify the remaining corpus."""
    ns = {"x": "http://www.xbrl.org/2003/instance", "d": "http://xbrl.org/2006/xbrldi"}
    results = []
    with ZipFile(path) as outer:
        manifest = json.loads(outer.read("manifest.json"))
        if manifest["financial_archive_sha256"] != financials["archive_sha256"]:
            raise ValueError("original source bound to different financial archive")
        for member, entry in manifest["entries"].items():
            raw = outer.read(member)
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("original source digest differs")
            record = next(r for r in financials["records"] if r.get("receipt") == entry["receipt"]
                          and r["ticker"] == entry["ticker"])
            if record["fs_div"] != "CFS":
                raise ValueError("original sample requires consolidated statements")
            with ZipFile(io.BytesIO(raw)) as inner:
                names = [i for i in inner.infolist() if i.filename.endswith(".xbrl")]
                if len(names) != 1 or names[0].file_size > 20_000_000:
                    raise ValueError("original XBRL scope differs")
                root = ET.fromstring(inner.read(names[0]))
            contexts = {e.get("id"): e for e in root.findall("x:context", ns)}
            units = {u.get("id"): u.findtext("x:measure", namespaces=ns) for u in root.findall("x:unit", ns)}
            values = {}
            for element in root:
                concept = element.tag.rsplit("}", 1)[-1]
                if concept not in ("Equity", "Liabilities", "ProfitLoss", "Revenue") or "ifrs-full}" not in element.tag:
                    continue
                context = contexts[element.get("contextRef")]
                dims = [(e.get("dimension"), e.text) for e in context.findall(".//d:explicitMember", ns)]
                if (dims != [("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", "ifrs-full:ConsolidatedMember")]
                        or context.findtext("x:entity/x:identifier", namespaces=ns) != record["corp_code"]
                        or units.get(element.get("unitRef")) != "iso4217:KRW"):
                    continue
                end = (context.findtext("x:period/x:instant", namespaces=ns)
                       or context.findtext("x:period/x:endDate", namespaces=ns))
                start = context.findtext("x:period/x:startDate", namespaces=ns)
                if not end or end[4:] != "-12-31" or (start and start != end[:4] + "-01-01"):
                    continue
                if end[:4] not in (str(record["year"]), str(record["year"] - 1)):
                    continue
                key = concept, int(end[:4])
                value = Decimal(element.text)
                if not value.is_finite() or key in values and values[key] != value:
                    raise ValueError("conflicting original XBRL totals")
                values[key] = value
            y = record["year"]
            equity, ni = values["Equity", y], values["ProfitLoss", y]
            original = {"equity": float(equity), "net_income": float(ni),
                        "roe": round(float(ni / equity * 100), 2),
                        "debt_ratio": round(float(values["Liabilities", y] / equity * 100), 2),
                        "revenue_growth": round(float((values["Revenue", y] / values["Revenue", y-1] - 1) * 100), 2)}
            if any(record["metrics"].get(k) != v for k, v in original.items()):
                raise ValueError("DART API and original XBRL financial totals differ")
            results.append({"ticker": record["ticker"], "receipt": record["receipt"], "year": y,
                            "matched_metrics": original, "raw_sha256": entry["sha256"],
                            "fs_div": "CFS", "currency": "KRW", "full_correction_chain_verified": False})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite financial source")
    config.load_env()
    files, manifest = acquire()
    write_archive(args.output, files, manifest)
    checked = load(args.output)
    print(json.dumps({"output": str(args.output), "sha256": checked["archive_sha256"],
                      "logical_requests": checked["logical_requests"], "status_counts": checked["status_counts"],
                      "unmapped": checked["unmapped_tickers"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
