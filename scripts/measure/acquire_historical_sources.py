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
import json
import os
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from signal_desk import config, market_clock  # noqa: E402

KRX_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
DART_URL = "https://opendart.fss.or.kr/api/list.json"
REQUEST_GAP_SECONDS = 0.5


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
    if first > last or end >= _registered_start():
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
    parser.add_argument("source", choices=("krx-daily", "dart-filings"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--detail", choices=("A001", "A002", "A003"))
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_requests > 100:
        parser.error("--max-requests must be between 1 and 100")
    if args.source == "dart-filings" and not args.detail:
        parser.error("--detail is required for DART")
    if args.output.exists():
        parser.error("archive already exists; retrieval never overwrites evidence")
    config.load_env()
    if args.source == "krx-daily":
        files, manifest = acquire_krx(args.start, args.end, max_requests=args.max_requests)
    else:
        files, manifest = acquire_dart(args.start, args.end, args.detail,
                                       max_requests=args.max_requests)
    write_archive(args.output, files, manifest)
    print(json.dumps({"output": str(args.output), "source": manifest["source"],
                      "request_count": manifest["request_count"],
                      "rows": sum(item["rows"] for item in manifest["entries"].values()),
                      "strict_pit_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
