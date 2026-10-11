"""Freeze bounded pre-registration KRX/DART responses for offline research.

Examples (never writes to the live cache or runs the registered harness):
  .venv/bin/python scripts/measure/acquire_historical_sources.py krx-daily \
    --start 2026-06-01 --end 2026-07-31 \
    --output data/research/krx-daily-2026-06-07-v1.zip
  .venv/bin/python scripts/measure/acquire_historical_sources.py dart-filings \
    --start 2026-03-01 --end 2026-03-31 --detail A001 \
    --output data/research/dart-annual-2026-03-v1.zip

Raw API response bytes and a hash/observation manifest are archived. Historical
retrieval is not evidence that Signal Desk observed the data at the old date.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from html.parser import HTMLParser
from math import gcd
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from signal_desk import config, market_clock  # noqa: E402
from signal_desk.broker import kis  # noqa: E402
from signal_desk.signals.research_action_coverage import (  # noqa: E402
    ActionRecord, AvailabilityEvidence, CoverageCertificate, SourceWindow,
)

KRX_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
DART_URL = "https://opendart.fss.or.kr/api/list.json"
DART_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
REQUEST_GAP_SECONDS = 0.5
KIS_SCHEDULES = {
    "dividend": ("/uapi/domestic-stock/v1/ksdinfo/dividend", "HHKDB669102C0"),
    "rev-split": ("/uapi/domestic-stock/v1/ksdinfo/rev-split", "HHKDB669105C0"),
}
KIND_DOCUMENT_URL = re.compile(
    r"https://kind\.krx\.co\.kr/external/(20\d{2})/(\d{2})/(\d{2})/"
    r"\d{6}/(\d{14})/[A-Za-z0-9]+\.htm\Z")


class SourceError(RuntimeError):
    """Never include a request URL: DART puts the API key in its query string."""


def _registered_start() -> str:
    spec = tomllib.loads((ROOT / "docs" / "preregistered.toml").read_text(encoding="utf-8"))
    dates = [str(item["registered_at"]) for item in spec.get("looks", [])
             if item.get("registered_at")]
    for family in spec.get("families", []):
        dates.extend(str(item["registered_at"]) for item in family.get("looks", [])
                     if item.get("registered_at"))
    if not dates:
        raise ValueError("registered-period protection has no starting date")
    return min(dates)


def _range(start: str, end: str) -> tuple[dt.date, dt.date]:
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first > last or last >= dt.date.fromisoformat(_registered_start()):
        raise ValueError("research range must end before the registered period")
    return first, last


def _fetch_json(url: str, params: dict, headers: dict, source: str) -> tuple[bytes, dict]:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and attempt < 2:
                time.sleep(attempt + 1)
                continue
            raise SourceError(f"{source}: HTTP {exc.code}") from None
        except Exception as exc:  # noqa: BLE001 — never leak a credential-bearing URL
            raise SourceError(f"{source}: {type(exc).__name__}") from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            if attempt < 2:
                time.sleep(attempt + 1)
                continue
            raise SourceError(f"{source}: invalid JSON after three attempts") from None
        if not isinstance(body, dict):
            raise SourceError(f"{source}: expected JSON object")
        return raw, body
    raise SourceError(f"{source}: unavailable")


def _entry(raw: bytes, *, rows: int) -> dict:
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "rows": rows, "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}


def _fetch_dart_document(receipt: str, key: str) -> bytes:
    """Preserve the complete original ZIP; never return an error XML as a document."""
    request = urllib.request.Request(
        f"{DART_DOCUMENT_URL}?{urllib.parse.urlencode({'rcept_no': receipt, 'crtfc_key': key})}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(15_000_001)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"DART document: HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 — URL contains the credential
        raise SourceError(f"DART document: {type(exc).__name__}") from None
    if len(raw) > 15_000_000:
        raise SourceError("DART document: response size limit")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if (len(names) != 1 or names[0] != f"{receipt}.xml"
                    or sum(item.file_size for item in archive.infolist()) > 15_000_000):
                raise SourceError("DART document: invalid archive members")
            for name in names:
                if not archive.read(name):
                    raise SourceError("DART document: empty archive member")
    except (BadZipFile, OSError, ValueError, EOFError):
        raise SourceError("DART document: invalid ZIP") from None
    return raw


def acquire_dart_actions(corp_code: str, start: str, end: str, *,
                         max_list_pages: int = 20, max_docs: int = 60,
                         gap_seconds: float = REQUEST_GAP_SECONDS) -> tuple[dict[str, bytes], dict]:
    """All filing categories for one issuer and receipt window, including corrections.

    An exhaustive *filing* archive is not an exhaustive effective-date action
    feed. 013 is preserved as an empty source response, not an action-free day.
    """
    first, last = _range(start, end)
    if (last - first).days > 31 or len(corp_code) != 8 or not corp_code.isdigit():
        raise ValueError("DART actions need an eight-digit issuer and at most 32 calendar days")
    if max_list_pages < 1 or max_docs < 1:
        raise ValueError("DART caps must be positive")
    key = config.dart_key()
    if not key:
        raise SourceError("DART API key unavailable")
    params = {"corp_code": corp_code, "bgn_de": start.replace("-", ""),
              "end_de": end.replace("-", ""), "last_reprt_at": "N",
              "sort": "date", "sort_mth": "asc", "page_count": "100"}
    files, entries, receipts = {}, {}, set()
    page, total_count, total_pages = 1, None, None
    while True:
        if page > max_list_pages:
            raise SourceError("DART actions: list page cap reached; no partial archive")
        raw, body = _fetch_json(DART_URL, {**params, "page_no": str(page),
                                          "crtfc_key": key}, {}, "DART actions")
        status = body.get("status")
        if status == "013" and page == 1:
            path = "raw/list/page-001.json"
            files[path] = raw
            entries[path] = {**_entry(raw, rows=0), "page": 1}
            return files, {"schema": "historical-action-source-v1", "source": "OpenDART",
                           "endpoint": "list.json + document.xml", "market": "kr", "corp_code": corp_code,
                           "start": start, "end": end, "query_date_basis": "receipt",
                           "query": params, "request_count": 1, "total_filings": 0,
                           "source_state": "source_empty_not_action_free", "entries": entries,
                           "strict_pit_eligible": False, "total_return_coverage": False}
        if status != "000":
            raise SourceError(f"DART actions: response code {status} on page {page}")
        rows = body.get("list")
        try:
            count, pages = int(body["total_count"]), int(body["total_page"])
        except (KeyError, ValueError, TypeError):
            raise SourceError("DART actions: missing pagination metadata") from None
        expected_rows = min(100, count - 100 * (page - 1))
        if (count < 1 or count > max_docs or pages != (count + 99) // 100
                or pages > max_list_pages or not isinstance(rows, list)
                or len(rows) != expected_rows
                or (total_count is not None and (count, pages) != (total_count, total_pages))):
            raise SourceError(f"DART actions: incomplete or capped list on page {page}")
        total_count, total_pages = count, pages
        for row in rows:
            receipt = row.get("rcept_no") if isinstance(row, dict) else None
            day = row.get("rcept_dt") if isinstance(row, dict) else None
            if (not isinstance(receipt, str) or len(receipt) != 14 or not receipt.isdigit()
                    or receipt in receipts or not isinstance(day, str)
                    or not params["bgn_de"] <= day <= params["end_de"]
                    or row.get("corp_code") != corp_code):
                raise SourceError(f"DART actions: invalid receipt, issuer or date on page {page}")
            receipts.add(receipt)
        path = f"raw/list/page-{page:03d}.json"
        files[path] = raw
        entries[path] = {**_entry(raw, rows=len(rows)), "page": page}
        if page == pages:
            break
        page += 1
        time.sleep(gap_seconds)
    if len(receipts) != total_count:
        raise SourceError("DART actions: receipt count disagrees with total")
    for index, receipt in enumerate(sorted(receipts)):
        if index:
            time.sleep(gap_seconds)
        raw = _fetch_dart_document(receipt, key)
        path = f"raw/doc/{receipt}.zip"
        files[path] = raw
        entries[path] = {**_entry(raw, rows=1), "rcept_no": receipt}
    return files, {"schema": "historical-action-source-v1", "source": "OpenDART",
                   "endpoint": "list.json + document.xml", "market": "kr", "corp_code": corp_code,
                   "start": start, "end": end, "query_date_basis": "receipt",
                   "query": params, "request_count": total_pages + total_count,
                   "total_filings": total_count, "source_state": "filings_archived_not_action_complete",
                   "entries": entries, "strict_pit_eligible": False,
                   "total_return_coverage": False}


def _fetch_kis_schedule_page(path: str, tr_id: str, params: dict, creds: dict,
                             tr_cont: str) -> tuple[bytes, dict, str]:
    token = kis.get_token(creds)
    if not token:
        raise SourceError("KIS schedule: token unavailable")
    base = {"demo": "https://openapivts.koreainvestment.com:29443",
            "real": "https://openapi.koreainvestment.com:9443"}.get(creds.get("env"))
    if not base:
        raise SourceError("KIS schedule: invalid environment")
    request = urllib.request.Request(f"{base}{path}?{urllib.parse.urlencode(params)}", headers={
        "authorization": f"Bearer {token}", "appkey": creds["app_key"],
        "appsecret": creds["app_secret"], "tr_id": tr_id, "custtype": "P",
        "tr_cont": tr_cont, "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(5_000_001)
            continuation = response.headers.get("tr_cont", "").strip().upper()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"KIS schedule: HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 — never print token, key or URL
        raise SourceError(f"KIS schedule: {type(exc).__name__}") from None
    if len(raw) > 5_000_000:
        raise SourceError("KIS schedule: response size limit")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SourceError("KIS schedule: invalid JSON") from None
    if not isinstance(body, dict):
        raise SourceError("KIS schedule: expected JSON object")
    return raw, body, continuation


def acquire_kis_schedule(kind: str, ticker: str, start: str, end: str, *,
                         max_requests: int = 10,
                         gap_seconds: float = REQUEST_GAP_SECONDS) -> tuple[dict[str, bytes], dict]:
    """Read-only KSD-family schedule sample; no ex-date or retention guarantee."""
    first, last = _range(start, end)
    if (last - first).days > 31 or kind not in KIS_SCHEDULES or len(ticker) != 6 or not ticker.isdigit():
        raise ValueError("KIS schedule needs a six-digit stock and at most 32 calendar days")
    if max_requests < 1:
        raise ValueError("KIS page cap must be positive")
    creds = config.kis_credentials()
    if not creds:
        raise SourceError("KIS schedule: credentials unavailable")
    path, tr_id = KIS_SCHEDULES[kind]
    params = ({"CTS": "", "GB1": "0", "F_DT": start.replace("-", ""),
               "T_DT": end.replace("-", ""), "SHT_CD": ticker, "HIGH_GB": ""}
              if kind == "dividend" else
              {"SHT_CD": ticker, "CTS": "", "F_DT": start.replace("-", ""),
               "T_DT": end.replace("-", ""), "MARKET_GB": "0"})
    files, entries, seen_hashes = {}, {}, set()
    page = 1
    while True:
        if page > max_requests:
            raise SourceError("KIS schedule: page cap reached; no partial archive")
        raw, body, continuation = _fetch_kis_schedule_page(
            path, tr_id, params, creds, "N" if page > 1 else "")
        if str(body.get("rt_cd")) != "0":
            raise SourceError(f"KIS schedule: response code {body.get('rt_cd')} on page {page}")
        rows = body.get("output1")
        if rows is None:
            rows = []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SourceError(f"KIS schedule: malformed rows on page {page}")
        if any(row.get("sht_cd") != ticker for row in rows):
            raise SourceError(f"KIS schedule: different security on page {page}")
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            raise SourceError("KIS schedule: repeated page; continuation did not advance")
        seen_hashes.add(digest)
        if continuation not in ("M", "", "D", "E", "F"):
            raise SourceError(f"KIS schedule: unknown continuation header on page {page}")
        if continuation == "M" and not rows:
            raise SourceError("KIS schedule: empty continuing page")
        name = f"raw/page-{page:03d}.json"
        files[name] = raw
        entries[name] = {**_entry(raw, rows=len(rows)), "page": page,
                         "tr_cont": continuation}
        if continuation != "M":
            break
        page += 1
        time.sleep(gap_seconds)
    total_rows = sum(item["rows"] for item in entries.values())
    return files, {"schema": "historical-action-source-v1", "source": "KIS KSD schedule",
                   "upstream_family": "KSD", "endpoint": path, "market": "kr",
                   "ticker": ticker, "start": start, "end": end,
                   "query_date_basis": "unverified_KIS_schedule_date",
                   "query": params, "request_count": page,
                   "total_rows": total_rows,
                   "source_state": ("schedule_archived_not_action_complete" if total_rows else
                                    "source_empty_not_action_free"),
                   "entries": entries, "strict_pit_eligible": False,
                   "total_return_coverage": False}


def acquire_kind_reviewed(plan: Path, *, max_requests: int = 12,
                          gap_seconds: float = REQUEST_GAP_SECONDS) -> tuple[dict[str, bytes], dict]:
    """Archive an explicit, bounded set of KIND originals, not a complete filing feed."""
    specification = json.loads(plan.read_text(encoding="utf-8"))
    documents = specification.get("documents")
    if (specification.get("schema") != "research-kind-review-plan-v1"
            or not isinstance(documents, list) or not 1 <= len(documents) <= max_requests <= 12):
        raise ValueError("KIND review: invalid plan or request cap")
    files, entries, seen_ids, seen_urls = {}, {}, set(), set()
    for index, item in enumerate(documents):
        if not isinstance(item, dict):
            raise ValueError("KIND review: invalid document")
        identifier, url, marker = (item.get("id"), item.get("url"),
                                   item.get("expected_text"))
        match = KIND_DOCUMENT_URL.fullmatch(url) if isinstance(url, str) else None
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9-]{3,64}", identifier)
                or identifier in seen_ids or url in seen_urls or not match
                or match[4][:8] != f"{match[1]}{match[2]}{match[3]}"
                or f"{match[1]}-{match[2]}-{match[3]}" >= _registered_start()
                or not isinstance(marker, str) or len(marker) < 3):
            raise ValueError("KIND review: invalid or duplicate id, URL, date or identity marker")
        seen_ids.add(identifier)
        seen_urls.add(url)
        request = urllib.request.Request(url, headers={"User-Agent": "signal-desk-research/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.geturl() != url or "text/html" not in response.headers.get("Content-Type", ""):
                    raise SourceError("KIND review: redirected or non-HTML response")
                raw = response.read(10_000_001)
        except urllib.error.HTTPError as exc:
            raise SourceError(f"KIND review: HTTP {exc.code}") from None
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — avoid writing partial evidence
            raise SourceError(f"KIND review: {type(exc).__name__}") from None
        if len(raw) > 10_000_000:
            raise SourceError("KIND review: response size limit")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError("KIND review: original is not UTF-8") from None
        if marker not in decoded or "<html" not in decoded.lower():
            raise SourceError("KIND review: issuer identity or HTML structure differs")
        name = f"raw/{identifier}.htm"
        files[name] = raw
        entries[name] = {**_entry(raw, rows=1), "url": url,
                         "document_id": identifier, "source_filing_date":
                         f"{match[1]}-{match[2]}-{match[3]}",
                         "expected_text": marker}
        if index + 1 < len(documents):
            time.sleep(gap_seconds)
    return files, {"schema": "historical-action-source-v1", "source": "KRX KIND originals",
                   "endpoint": "selected /external/ HTML documents", "market": "kr",
                   "request_count": len(documents), "source_state":
                   "selected_originals_not_action_complete", "entries": entries,
                   "strict_pit_eligible": False, "total_return_coverage": False}


def verify_action_archive(path: Path) -> dict:
    """Replay a local original-byte bundle without contacting a provider."""
    with ZipFile(path) as archive:
        names = archive.namelist()
        if names.count("manifest.json") != 1 or len(names) != len(set(names)):
            raise SourceError("action archive: invalid member inventory")
        manifest = json.loads(archive.read("manifest.json"))
        entries = manifest.get("entries")
        if (manifest.get("schema") != "historical-action-source-v1"
                or not isinstance(entries, dict)
                or set(names) != set(entries) | {"manifest.json"}
                or manifest.get("strict_pit_eligible") is not False
                or manifest.get("total_return_coverage") is not False):
            raise SourceError("action archive: invalid manifest or coverage claim")
        if manifest.get("source") == "KRX KIND originals" and (
                not 1 <= len(entries) <= 12
                or any(archive.getinfo(name).file_size > 10_000_000 for name in entries)):
            raise SourceError("action archive: KIND member count or size limit")
        for name, entry in entries.items():
            raw = archive.read(name)
            if (not name.startswith("raw/") or hashlib.sha256(raw).hexdigest() != entry.get("sha256")
                    or len(raw) != entry.get("bytes")):
                raise SourceError(f"action archive: source hash/length mismatch at {name}")
            if manifest.get("source") == "KRX KIND originals":
                match = KIND_DOCUMENT_URL.fullmatch(entry.get("url", ""))
                if (not match or name != f"raw/{entry.get('document_id')}.htm"
                        or match[4][:8] != f"{match[1]}{match[2]}{match[3]}"
                        or entry.get("source_filing_date") != f"{match[1]}-{match[2]}-{match[3]}"):
                    raise SourceError(f"action archive: KIND identity mismatch at {name}")
                try:
                    decoded = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise SourceError(f"action archive: KIND encoding mismatch at {name}") from None
                marker = entry.get("expected_text")
                if (not isinstance(marker, str) or len(marker) < 3
                        or marker not in decoded or "<html" not in decoded.lower()):
                    raise SourceError(f"action archive: KIND issuer or HTML mismatch at {name}")
        return {"source": manifest["source"], "source_state": manifest["source_state"],
                "raw_members": len(entries), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "strict_pit_eligible": False, "total_return_coverage": False}


def source_window_from_archive(path: Path) -> SourceWindow:
    """Describe *collected* scope without certifying action-free sessions."""
    checked = verify_action_archive(path)
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    is_dart = manifest["source"] == "OpenDART"
    if not is_dart and manifest["source"] != "KIS KSD schedule":
        raise SourceError("action archive: unknown upstream family")
    entries = manifest["entries"]
    observed = max(dt.datetime.fromisoformat(entry["retrieved_at_utc"])
                   for entry in entries.values())
    state = ("source_empty" if manifest["source_state"] == "source_empty_not_action_free"
             else "complete_for_source")
    return SourceWindow(
        "kr", (f"corp:{manifest['corp_code']}" if is_dart else
               f"ticker:{manifest['ticker']}"), checked["sha256"],
        "DART" if is_dart else "KSD", manifest["start"], manifest["end"],
        "receipt" if is_dart else "unknown",
        frozenset({"unsupported"} if is_dart else
                  {"cash_dividend" if manifest["endpoint"].endswith("/dividend") else "split"}),
        state, tuple(entry["sha256"] for _, entry in sorted(entries.items())), observed,
        historical_range_confirmed=False)


class _DocumentText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def extract_dart_common_dividend(path: Path, receipt: str) -> ActionRecord:
    """Parse one known DART cash resolution as an *announced schedule*.

    This intentionally does not infer ex-date, actual payment, or preferred
    security identity from issuer-level metadata. Ambiguous templates fail.
    """
    verify_action_archive(path)
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["source"] != "OpenDART":
            raise SourceError("DART dividend: wrong archive family")
        matches = []
        for name in sorted(manifest["entries"]):
            if not name.startswith("raw/list/"):
                continue
            page = json.loads(archive.read(name))
            matches.extend(row for row in page.get("list", [])
                           if row.get("rcept_no") == receipt)
        if len(matches) != 1 or matches[0].get("report_nm", "").strip() != "현금ㆍ현물배당결정":
            raise SourceError("DART dividend: receipt is missing, corrected or not a cash resolution")
        row = matches[0]
        ticker = row.get("stock_code")
        if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
            raise SourceError("DART dividend: security identity is unavailable")
        name = f"raw/doc/{receipt}.zip"
        if name not in manifest["entries"]:
            raise SourceError("DART dividend: original document is missing")
        with ZipFile(io.BytesIO(archive.read(name))) as original:
            members = original.namelist()
            if len(members) != 1 or not members[0].startswith(receipt):
                raise SourceError("DART dividend: ambiguous original document")
            raw = original.read(members[0])
        observed = dt.datetime.fromisoformat(manifest["entries"][name]["retrieved_at_utc"])
        source_hash = manifest["entries"][name]["sha256"]
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp949")
        except UnicodeDecodeError:
            raise SourceError("DART dividend: undecodable document") from None
    parser = _DocumentText()
    parser.feed(decoded)
    text = re.sub(r"\s+", " ", " ".join(parser.parts))

    def field(pattern: str) -> str:
        found = re.findall(pattern, text)
        if len(found) != 1:
            raise SourceError("DART dividend: ambiguous or missing original field")
        return found[0]

    amount = int(field(r"3\.\s*1주당 배당금\(원\)\s*보통주식\s*([0-9,]+)").replace(",", ""))
    record_date = field(r"6\.\s*배당기준일\s*(\d{4}-\d{2}-\d{2})")
    scheduled_payment = field(r"7\.\s*배당금지급 예정일자\s*(\d{4}-\d{2}-\d{2})")
    decision_date = field(r"10\.\s*이사회결의일\(결정일\)\s*(\d{4}-\d{2}-\d{2})")
    received = dt.datetime.strptime(row["rcept_dt"], "%Y%m%d").date().isoformat()
    if decision_date > received:
        raise SourceError("DART dividend: board decision follows filing receipt")
    availability = AvailabilityEvidence(received, observed, availability_kind="retrospective")
    return ActionRecord("kr", f"ticker:{ticker}", f"DART:{receipt}:common",
                        "cash_dividend", None, "scheduled", f"DART:{receipt}",
                        source_hash, availability, issuer_name=row.get("corp_name"),
                        share_class="common", currency="KRW",
                        cash_per_share=amount, record_date=record_date,
                        scheduled_payment_date=scheduled_payment,
                        source_span="items 3, 6, 7 and 10 of cash dividend resolution")


def _kis_iso_date(value: object) -> str | None:
    raw = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def extract_kis_schedule_facts(path: Path) -> tuple[ActionRecord, ...]:
    """Keep schedule facts retrospective; KIS does not expose old publication time."""
    verify_action_archive(path)
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["source"] != "KIS KSD schedule":
            raise SourceError("KIS facts: wrong archive family")
        kind = ("cash_dividend" if manifest["endpoint"].endswith("/dividend") else
                "split" if manifest["endpoint"].endswith("/rev-split") else None)
        if kind is None:
            raise SourceError("KIS facts: unsupported schedule endpoint")
        facts = []
        for name, entry in sorted(manifest["entries"].items()):
            body = json.loads(archive.read(name))
            rows = body.get("output1") or []
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                raise SourceError("KIS facts: malformed saved rows")
            observed = dt.datetime.fromisoformat(entry["retrieved_at_utc"])
            availability = AvailabilityEvidence(None, observed, availability_kind="retrospective")
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or row.get("sht_cd") != manifest["ticker"]:
                    raise SourceError("KIS facts: saved row security mismatch")
                if kind == "cash_dividend":
                    if not str(row.get("stk_kind") or "").startswith("보통"):
                        raise SourceError("KIS facts: preferred/share-class mapping unresolved")
                    record = _kis_iso_date(row.get("record_date"))
                    payment = _kis_iso_date(row.get("divi_pay_dt"))
                    amount_text = str(row.get("per_sto_divi_amt") or "").replace(",", "")
                    stock_rate = str(row.get("stk_divi_rate") or "0").replace(",", "")
                    try:
                        amount, stock_fraction = float(amount_text), float(stock_rate)
                    except ValueError:
                        raise SourceError("KIS facts: invalid dividend amount/rate") from None
                    if not record or not payment or stock_fraction != 0:
                        raise SourceError("KIS facts: incomplete or mixed stock dividend terms")
                    facts.append(ActionRecord(
                        "kr", f"ticker:{manifest['ticker']}",
                        f"KIS:dividend:{manifest['ticker']}:{record}:{name}:{index}",
                        "cash_dividend", None, "scheduled", f"KIS:{name}", entry["sha256"],
                        availability, issuer_name=row.get("isin_name"), share_class="common",
                        currency="KRW", cash_per_share=amount, record_date=record,
                        scheduled_payment_date=payment,
                        source_span="record_date, per_sto_divi_amt, divi_pay_dt, stk_kind"))
                else:
                    record = _kis_iso_date(row.get("record_date"))
                    listing = _kis_iso_date(row.get("list_dt"))
                    try:
                        before = int(str(row.get("inter_bf_face_amt") or ""))
                        after = int(str(row.get("inter_af_face_amt") or ""))
                    except ValueError:
                        raise SourceError("KIS facts: invalid face-value ratio") from None
                    if not record or not listing or before < 1 or after < 1:
                        raise SourceError("KIS facts: incomplete split terms")
                    factor = gcd(before, after)
                    facts.append(ActionRecord(
                        "kr", f"ticker:{manifest['ticker']}",
                        f"KIS:rev-split:{manifest['ticker']}:{record}:{name}:{index}",
                        "split", None, "scheduled", f"KIS:{name}", entry["sha256"],
                        availability, issuer_name=row.get("isin_name"),
                        split_numerator=before // factor, split_denominator=after // factor,
                        record_date=record, listing_session=listing,
                        source_span="record_date, face values, listing date; trade halt in raw response"))
    return tuple(facts)


def compare_dart_kis_dividend(dart: ActionRecord, kis_archive: Path) -> dict:
    """Match factual schedule fields without promoting a payment or ex-date."""
    if (dart.market != "kr" or dart.kind != "cash_dividend"
            or dart.share_class != "common" or not dart.security_id.startswith("ticker:")
            or not dart.issuer_name or not dart.record_date
            or dart.cash_per_share is None or not dart.scheduled_payment_date):
        raise ValueError("DART common dividend terms are insufficient for comparison")
    checked = verify_action_archive(kis_archive)
    window = source_window_from_archive(kis_archive)
    if checked["source"] != "KIS KSD schedule" or not window.source_id:
        raise SourceError("dividend comparison needs a saved KIS schedule")
    if window.security_id != dart.security_id or not window.source_id:
        raise SourceError("dividend comparison security identity differs")
    with ZipFile(kis_archive) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if not manifest["endpoint"].endswith("/dividend"):
            raise SourceError("dividend comparison needs a dividend schedule")
        rows = []
        for name in sorted(manifest["entries"]):
            body = json.loads(archive.read(name))
            page_rows = body.get("output1") or []
            if isinstance(page_rows, dict):
                page_rows = [page_rows]
            rows.extend(page_rows)
    selected = [row for row in rows if (
        row.get("sht_cd") == dart.security_id.removeprefix("ticker:")
        and str(row.get("stk_kind") or "").startswith("보통")
        and _kis_iso_date(row.get("record_date")) == dart.record_date)]
    if len(selected) != 1:
        return {"state": "missing_or_ambiguous", "matches": len(selected),
                "dart_event_id": dart.event_id, "kis_archive_sha256": checked["sha256"],
                "total_return_coverage": False, "strict_pit_eligible": False}
    row = selected[0]
    try:
        amount = float(str(row.get("per_sto_divi_amt", "")).replace(",", ""))
    except ValueError:
        amount = None
    observed = {"issuer_name": str(row.get("isin_name") or "").strip(),
                "cash_per_share": amount,
                "scheduled_payment_date": _kis_iso_date(row.get("divi_pay_dt"))}
    expected = {"issuer_name": dart.issuer_name.strip(),
                "cash_per_share": float(dart.cash_per_share),
                "scheduled_payment_date": dart.scheduled_payment_date}
    conflicts = [key for key in expected if expected[key] != observed[key]]
    return {"state": "conflict" if conflicts else "matched_schedule_fields",
            "conflicting_fields": conflicts, "dart_event_id": dart.event_id,
            "dart_original_sha256": dart.raw_sha256,
            "kis_archive_sha256": checked["sha256"],
            "record_date": dart.record_date, "amount_krw": dart.cash_per_share,
            "scheduled_payment_date": dart.scheduled_payment_date,
            "actual_payment_confirmed": False, "ex_date_confirmed": False,
            "total_return_coverage": False, "strict_pit_eligible": False}


def acquire_krx(start: str, end: str, *, max_requests: int = 60,
                gap_seconds: float = REQUEST_GAP_SECONDS) -> tuple[dict[str, bytes], dict]:
    first, last = _range(start, end)
    days = []
    day = first
    while day <= last:
        if market_clock.is_session("kr", day):
            days.append(day.isoformat())
        day += dt.timedelta(days=1)
    if not days or len(days) > max_requests:
        raise ValueError("KRX session count is empty or exceeds the request cap")
    key = config.krx_key()
    if not key:
        raise SourceError("KRX API key unavailable")
    files, entries = {}, {}
    for index, session in enumerate(days):
        bas_dd = session.replace("-", "")
        raw, body = _fetch_json(KRX_URL, {"basDd": bas_dd}, {"AUTH_KEY": key}, "KRX")
        if "respCode" in body:
            raise SourceError(f"KRX: response code {body['respCode']} on {session}")
        rows = body.get("OutBlock_1")
        if not isinstance(rows, list) or not rows:
            raise SourceError(f"KRX: empty/malformed session {session}; no price interpolation")
        codes = [str(row.get("ISU_CD") or "") for row in rows if isinstance(row, dict)]
        if (len(codes) != len(rows) or len(set(codes)) != len(codes)
                or any(row.get("BAS_DD") != bas_dd for row in rows)):
            raise SourceError(f"KRX: inconsistent date or duplicate code on {session}")
        path = f"raw/{session}.json"
        files[path] = raw
        entries[path] = {**_entry(raw, rows=len(rows)), "session": session,
                         "distinct_codes": len(codes)}
        if index + 1 < len(days):
            time.sleep(gap_seconds)
    manifest = {"schema": "historical-source-v1", "source": "KRX official Open API",
                "endpoint": "sto/stk_bydd_trd", "market": "kr", "start": start, "end": end,
                "expected_sessions": days, "request_count": len(days), "entries": entries,
                "level": "historical_retrieval_not_as_run", "strict_pit_eligible": False,
                "limitations": ["first observed now; old source-vintage and corporate actions unverified",
                                "daily records are raw KOSPI listings, not a historical KOSPI200 membership file",
                                "no prices or sessions are interpolated"]}
    return files, manifest


def acquire_dart(start: str, end: str, detail: str, *, max_requests: int = 30,
                 gap_seconds: float = REQUEST_GAP_SECONDS) -> tuple[dict[str, bytes], dict]:
    first, last = _range(start, end)
    if (last - first).days > 92 or detail not in {"A001", "A002", "A003"}:
        raise ValueError("DART window exceeds three months or report detail is unsupported")
    key = config.dart_key()
    if not key:
        raise SourceError("DART API key unavailable")
    params = {"bgn_de": start.replace("-", ""), "end_de": end.replace("-", ""),
              "pblntf_ty": "A", "pblntf_detail_ty": detail, "corp_cls": "Y",
              "last_reprt_at": "N", "sort": "date", "sort_mth": "asc", "page_count": "100"}
    files, entries, receipts, total_count, total_pages = {}, {}, set(), None, None
    page = 1
    while True:
        if page > max_requests:
            raise SourceError("DART: page count exceeds request cap; no partial archive")
        raw, body = _fetch_json(DART_URL, {**params, "page_no": str(page),
                                          "crtfc_key": key}, {}, "DART")
        if body.get("status") != "000":
            raise SourceError(f"DART: response code {body.get('status')} on page {page}")
        rows = body.get("list")
        try:
            body_count, body_pages = int(body["total_count"]), int(body["total_page"])
        except (KeyError, ValueError, TypeError):
            raise SourceError("DART: missing pagination metadata") from None
        if (not isinstance(rows, list) or not rows or body_count < len(rows)
                or body_pages < 1 or body_pages > max_requests
                or (total_count is not None and (body_count, body_pages) != (total_count, total_pages))):
            raise SourceError(f"DART: incomplete or changing pagination on page {page}")
        total_count, total_pages = body_count, body_pages
        for row in rows:
            receipt = row.get("rcept_no") if isinstance(row, dict) else None
            day = str(row.get("rcept_dt") or "") if isinstance(row, dict) else ""
            if (not isinstance(receipt, str) or len(receipt) != 14 or receipt in receipts
                    or not params["bgn_de"] <= day <= params["end_de"]):
                raise SourceError(f"DART: invalid/duplicate receipt or date on page {page}")
            receipts.add(receipt)
        path = f"raw/page-{page:03d}.json"
        files[path] = raw
        entries[path] = {**_entry(raw, rows=len(rows)), "page": page}
        if page >= total_pages:
            break
        page += 1
        time.sleep(gap_seconds)
    if len(receipts) != total_count:
        raise SourceError("DART: receipt total disagrees with pagination")
    manifest = {"schema": "historical-source-v1", "source": "OpenDART official API",
                "endpoint": "list.json", "market": "kr", "start": start, "end": end,
                "query": params, "request_count": total_pages, "total_filings": total_count,
                "distinct_receipts": len(receipts), "entries": entries,
                "level": "historical_retrieval_not_as_run", "strict_pit_eligible": False,
                "limitations": ["receipt date is verified but receipt time is not in list.json",
                                "list is metadata, not the historical filing document or factor input",
                                "all corrections are retained; latest-only selection is disabled"]}
    return files, manifest


def write_archive(output: Path, files: dict[str, bytes], manifest: dict) -> None:
    """Write only after every requested page/date passed validation; never overwrite."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.",
                                     suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with ZipFile(handle, "w", compression=ZIP_DEFLATED) as archive:
                for name, raw in sorted(files.items()):
                    archive.writestr(name, raw)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False,
                                                              sort_keys=True, indent=2))
            os.link(temporary, output)  # atomic no-clobber; never replace an earlier evidence bundle
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("krx-daily", "dart-filings", "dart-actions",
                                           "kind-reviewed", "verify-kind",
                                           "kis-dividend", "kis-rev-split", "verify-actions",
                                           "inspect-dividend", "inspect-kis", "compare-dividend"))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--detail", choices=("A001", "A002", "A003"))
    parser.add_argument("--corp-code", help="OpenDART eight-digit issuer identity")
    parser.add_argument("--ticker", help="KIS six-digit listed security")
    parser.add_argument("--receipt", help="DART filing receipt to inspect from a saved archive")
    parser.add_argument("--dart-archive", type=Path, help="saved DART action ZIP for comparison")
    parser.add_argument("--source-plan", type=Path, help="fixed KIND original-document review plan")
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--max-docs", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_requests > 100:
        parser.error("--max-requests must be between 1 and 100")
    if args.max_docs < 1 or args.max_docs > 100:
        parser.error("--max-docs must be between 1 and 100")
    if args.source == "verify-actions":
        checked = verify_action_archive(args.output)
        source_window = source_window_from_archive(args.output)
        scope = CoverageCertificate(
            source_window.market, source_window.security_id,
            source_window.start, source_window.end,
            frozenset({"cash_dividend", "split", "halt", "delisting"}),
            (source_window,))
        print(json.dumps({**checked, "coverage_state": scope.state,
                          "security_id": source_window.security_id,
                          "query_date_basis": source_window.date_basis}, ensure_ascii=False))
        return
    if args.source == "verify-kind":
        checked = verify_action_archive(args.output)
        if checked["source"] != "KRX KIND originals":
            raise SourceError("KIND review: wrong archive family")
        print(json.dumps(checked, ensure_ascii=False))
        return
    if args.source == "inspect-dividend":
        if not args.receipt or len(args.receipt) != 14 or not args.receipt.isdigit():
            parser.error("--receipt must be a fourteen-digit DART receipt")
        print(json.dumps(asdict(extract_dart_common_dividend(args.output, args.receipt)),
                         ensure_ascii=False, default=str, sort_keys=True))
        return
    if args.source == "inspect-kis":
        print(json.dumps({"facts": [asdict(row) for row in extract_kis_schedule_facts(args.output)],
                          "strict_pit_eligible": False, "total_return_coverage": False},
                         ensure_ascii=False, default=str, sort_keys=True))
        return
    if args.source == "compare-dividend":
        if not args.dart_archive or not args.receipt:
            parser.error("--dart-archive and --receipt are required for comparison")
        dart = extract_dart_common_dividend(args.dart_archive, args.receipt)
        print(json.dumps(compare_dart_kis_dividend(dart, args.output),
                         ensure_ascii=False, sort_keys=True, allow_nan=False))
        return
    if args.source != "kind-reviewed" and (not args.start or not args.end):
        parser.error("--start and --end are required for acquisition")
    if args.source == "kind-reviewed" and not args.source_plan:
        parser.error("--source-plan is required for KIND originals")
    if args.source == "dart-filings" and not args.detail:
        parser.error("--detail is required for DART")
    if args.source == "dart-actions" and not args.corp_code:
        parser.error("--corp-code is required for DART actions")
    if args.source.startswith("kis-") and not args.ticker:
        parser.error("--ticker is required for KIS schedules")
    if args.output.exists():
        parser.error("archive already exists; retrieval never overwrites evidence")
    config.load_env()
    if args.source == "krx-daily":
        files, manifest = acquire_krx(args.start, args.end, max_requests=args.max_requests)
    elif args.source == "dart-filings":
        files, manifest = acquire_dart(args.start, args.end, args.detail,
                                       max_requests=args.max_requests)
    elif args.source == "dart-actions":
        files, manifest = acquire_dart_actions(args.corp_code, args.start, args.end,
                                               max_list_pages=args.max_requests,
                                               max_docs=args.max_docs)
    elif args.source == "kind-reviewed":
        files, manifest = acquire_kind_reviewed(args.source_plan,
                                                max_requests=min(args.max_requests, 12))
    else:
        kind = "dividend" if args.source == "kis-dividend" else "rev-split"
        files, manifest = acquire_kis_schedule(kind, args.ticker, args.start, args.end,
                                               max_requests=args.max_requests)
    write_archive(args.output, files, manifest)
    print(json.dumps({"output": str(args.output), "source": manifest["source"],
                      "request_count": manifest["request_count"],
                      "rows": manifest.get("total_filings", manifest.get("total_rows", sum(
                          item["rows"] for item in manifest["entries"].values()))),
                      "source_state": manifest.get("source_state", "historical_retrieval_not_as_run"),
                      "strict_pit_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
