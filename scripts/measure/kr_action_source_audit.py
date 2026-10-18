"""Read-only, resumable public-source observations within the v2 action budget."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from scripts.measure.kr_net_alpha_sources import ROOT, DEST, _NoRedirect
from scripts.measure.kr_net_alpha_v2 import contract, source_storage_bytes, price_request_inventory, JOURNAL_SHA
from scripts.measure.acquire_historical_sources import _fetch_kis_schedule_page, write_archive
from scripts.measure.contract_event_sources import _Form, _text, SEARCH
from scripts.measure.kr_net_alpha_v2 import save
from signal_desk import config

HOSTS = frozenset({"kind.krx.co.kr", "seibro.or.kr", "www.seibro.or.kr", "api.seibro.or.kr",
                  "global.krx.co.kr", "open.krx.co.kr", "law.krx.co.kr", "www.samsung.com"})


def write_journal(path, rows):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    temporary.replace(path)


class Collector:
    def __init__(self):
        self.spec = contract()
        self.lock = (DEST/".collector.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path = DEST/"v2-action-requests.json"
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else []
        self.previous = 0.0

    def save(self):
        write_journal(self.path, self.journal)

    def kis_flags(self, ticker, start, end):
        if (len(ticker) != 6 or not ticker.isdigit() or not self.spec["warmup_from"] <= start <= end
                <= self.spec["liquidation_through"]):
            raise ValueError("KIS flag query outside fixed source scope")
        path = DEST/"v2-price-requests.json"
        journal = json.loads(path.read_text()) if path.exists() else []
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                  "FID_INPUT_DATE_1": start.replace("-", ""), "FID_INPUT_DATE_2": end.replace("-", ""),
                  "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        for row in journal:
            if row["request_key"] == key and row.get("response_path"):
                target = (DEST/row["response_path"]).resolve()
                if not target.is_relative_to(DEST.resolve()):
                    raise ValueError("response path outside research directory")
                raw = target.read_bytes()
                if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                    raise ValueError("saved KIS flags differ")
                return raw, row
        if (self.spec["amendment"]["source_scope"]["previous_price_metadata_requests"]+len(journal)
                >= self.spec["max_price_metadata_requests"]):
            raise ValueError("cumulative price request budget reached")
        if source_storage_bytes() >= self.spec["max_new_archive_bytes"]:
            raise ValueError("cumulative source byte cap reached")
        creds = config.kis_credentials()
        if not creds:
            raise ValueError("KIS credentials missing")
        time.sleep(max(0, 1-(time.monotonic()-self.previous)))
        row = {"source": "KIS original price and corporate flags", "request_key": key,
               "params": params, "state": "reserved", "environment": creds["env"],
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        journal.append(row)
        write_journal(path, journal)
        self.previous = time.monotonic()
        try:
            raw, body, cont = _fetch_kis_schedule_page(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100", params, creds, "")
            if source_storage_bytes()+len(raw) > self.spec["max_new_archive_bytes"]:
                raise ValueError("source byte budget reached")
            target = DEST/"effective-price-responses"/f"{len(journal):04d}.json"
            target.parent.mkdir(exist_ok=True)
            if target.exists(): raise ValueError("refusing to overwrite source bytes")
            target.write_bytes(raw)
            row.update(state="response_saved", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                       response_path=str(target.relative_to(DEST)), continuation=cont,
                       api_result_code=str(body.get("rt_cd")), economic_scope_certified=False)
            return raw, row
        except BaseException as exc:
            row.update(state="failed", error=type(exc).__name__); raise
        finally:
            write_journal(path, journal)

    def get(self, url, data=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password
                or any(word in k.lower() for k, _ in urllib.parse.parse_qsl(parsed.query)
                       for word in ("token", "secret", "servicekey", "auth_key"))
                or (data is not None and not (parsed.hostname == "kind.krx.co.kr"
                                             and parsed.path in ("/disclosure/details.do", "/disclosureinfo/dividendinfo.do")))):
            raise ValueError("URL outside read-only public-source scope")
        key = hashlib.sha256(url.encode()+b"\0"+(data or b"")).hexdigest()
        for row in reversed(self.journal):
            if row.get("request_key") == key and row.get("response_path"):
                path = (DEST/row["response_path"]).resolve()
                if not path.is_relative_to(DEST.resolve()):
                    raise ValueError("response path outside research directory")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                    raise ValueError("saved source response differs")
                if row["state"] != "response_saved":
                    raise ValueError(f"preserved source unavailable: {row.get('http_status', row['state'])}")
                return raw, row
        used = self.spec["amendment"]["source_scope"]["previous_action_requests"]+len(self.journal)
        if used >= self.spec["max_action_source_requests"]:
            raise ValueError("cumulative action request cap reached")
        if source_storage_bytes() >= self.spec["max_new_archive_bytes"]:
            raise ValueError("cumulative source byte cap reached")
        time.sleep(max(0, 1-(time.monotonic()-self.previous)))
        row = {"source": parsed.hostname, "url": url, "request_key": key,
               "method": "POST" if data is not None else "GET", "state": "reserved",
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        if data is not None:
            row["request_form"] = urllib.parse.parse_qs(data.decode(), keep_blank_values=True)
        self.journal.append(row); self.save(); self.previous = time.monotonic()
        try:
            req = urllib.request.Request(url, data, headers={"User-Agent": "signal-desk-research/1.0",
                "Referer": f"https://{parsed.hostname}/"})
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                response = opener.open(req, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(5_000_001)
                row["http_status"] = response.code
                row["content_type"] = response.headers.get("Content-Type", "")
                if response.headers.get("Location"):
                    row["location"] = response.headers["Location"]
            if len(raw) > 5_000_000 or source_storage_bytes()+len(raw) > self.spec["max_new_archive_bytes"]:
                raise ValueError("source byte cap reached")
            path = DEST/"effective-source-responses"/f"{len(self.journal):04d}.bin"
            path.parent.mkdir(exist_ok=True)
            if path.exists():
                raise ValueError("refusing to overwrite source bytes")
            path.write_bytes(raw)
            row.update(state="response_saved" if 200 <= row["http_status"] < 300 else "http_error",
                       bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                       response_path=str(path.relative_to(DEST)))
            if row["state"] != "response_saved":
                raise ValueError(f"source HTTP {row['http_status']}")
            return raw, row
        except BaseException as exc:
            if row["state"] == "reserved": row.update(state="failed", error=type(exc).__name__)
            raise
        finally:
            self.save()


def listing(source, start, end):
    """Keep market-wide announcements; company codes are NOT security ISINs."""
    total = re.search(r"전체\s*<em>([\d,]+)</em>", source)
    if not total:
        raise ValueError("KIND action list total missing")
    result = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, re.S | re.I):
        receipt = re.search(r"openDisclsViewer\('([0-9]{14})'", row)
        if not receipt:
            continue
        cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) < 5:
            raise ValueError("action list row malformed")
        stamp = dt.datetime.strptime(cells[1], "%Y-%m-%d %H:%M")
        if not start <= stamp.date().isoformat() <= end or not receipt[1].startswith(stamp.strftime("%Y%m%d")):
            raise ValueError("action list outside requested dates")
        code = re.search(r"companysummary_open\('([0-9A-Z]{5})'", row)
        result.append({"receipt": receipt[1], "published_at": stamp.isoformat()+"+09:00",
            "publication_precision": "minute", "company_code": code[1] if code else None,
            "issuer_display_today": cells[2], "title": cells[3], "submitter": cells[4],
            "identity_status": "company_code_not_security_binding" if code else "market_wide_or_unresolved"})
    return int(total[1].replace(",", "")), result


def census(collector, form, start, end, max_pages=20, term=""):
    spec = collector.spec
    if (not spec["warmup_from"] <= start <= end <= spec["liquidation_through"]
            or not 1 <= max_pages <= 30 or term not in ("", "주식분할", "배당")):
        raise ValueError("action census outside fixed source scope")
    fields = {**form, "method": "searchDetailsSub", "forward": "details_sub",
        "currentPageSize": "100", "fromDate": start, "toDate": end, "marketType": "1",
        "orderMode": "0", "orderStat": "D", "reportNm": term, "reportNmTemp": term}
    if not term:
        fields.update(disclosureType02="0303", disclosureTypeArr02="0303")
    rows, pages, expected = [], [], None
    for page in range(1, max_pages+1):
        raw, meta = collector.get(SEARCH, urllib.parse.urlencode({**fields, "pageIndex": str(page)}).encode())
        total, batch = listing(raw.decode(), start, end)
        if expected is not None and total != expected:
            raise ValueError("action list total changed")
        expected = total
        rows.extend(batch)
        pages.append({"response_path": meta["response_path"], "sha256": meta["sha256"],
                      "rows": len(batch), "declared_total": total})
        if len(rows) >= expected or not batch:
            break
    receipts = [r["receipt"] for r in rows]
    complete = len(rows) == expected and len(set(receipts)) == len(rows)
    return {"start": start, "end": end, "term": term,
        "category": None if term else "0303",
        "pages": pages, "declared_total": expected, "rows": rows,
        "state": "listing_complete_not_economic_scope" if complete else "listing_incomplete",
        "all_corporate_actions_certified": False, "correction_chain_reviewed": False}


def original(collector, receipt):
    """Resolve an exact as-published version, never silently pick latest correction."""
    if not re.fullmatch(r"[0-9]{14}", receipt):
        raise ValueError("invalid receipt")
    root = "https://kind.krx.co.kr/common/disclsviewer.do"
    shell, shell_meta = collector.get(root+"?method=search&acptno="+receipt)
    options = re.findall(r"<option\s+value=['\"]([0-9]{14})\|[YN]['\"][^>]*>(.*?)</option>", shell.decode(), re.S)
    # Preserve every version in the shell; callers must review related filings.
    matching = [(doc, _text(label)) for doc, label in options
                if dt.datetime.strptime(receipt[:8], "%Y%m%d").strftime("%Y.%m.%d") in _text(label)]
    if len(matching) != 1:
        raise ValueError("as-published document version ambiguous")
    doc, label = matching[0]
    resolver, resolver_meta = collector.get(root+"?method=searchContents&docNo="+doc)
    urls = set(re.findall(r"https://kind\.krx\.co\.kr/external/[0-9/]+/[0-9]+\.htm", resolver.decode()))
    expected = (f"https://kind.krx.co.kr/external/{receipt[:4]}/{receipt[4:6]}/"
                f"{receipt[6:8]}/{receipt[8:]}/{doc}/")
    if len(urls) != 1 or not next(iter(urls)).startswith(expected):
        raise ValueError("official original unresolved")
    raw, meta = collector.get(next(iter(urls)))
    return raw, {"receipt": receipt, "document_number": doc, "label": label,
        "versions_in_viewer": [{"document_number": d, "label": _text(t)} for d, t in options],
        "viewer": shell_meta, "resolver": resolver_meta, "original": meta,
        "correction_chain_reviewed": False}


def snapshot(collector, output):
    """Archive the actual investigation, including failures, not a scope certificate."""
    files, missing = {}, []
    parent = (DEST/"requests.json").read_bytes()
    if hashlib.sha256(parent).hexdigest() != JOURNAL_SHA:
        raise ValueError("pinned parent request ledger differs")
    for name in ("v2-action-requests.json", "v2-price-requests.json"):
        raw = (DEST/name).read_bytes()
        files["journals/"+name] = raw
        for i, row in enumerate(json.loads(raw), 1):
            if not row.get("response_path"):
                missing.append({"journal": name, "attempt": i, "state": row["state"]})
                continue
            source = (DEST/row["response_path"]).resolve()
            if not source.is_relative_to(DEST.resolve()):
                raise ValueError("source snapshot path outside research directory")
            body = source.read_bytes()
            if hashlib.sha256(body).hexdigest() != row["sha256"]:
                raise ValueError("snapshot response hash differs")
            files["responses/"+row["response_path"]] = body
    base = ROOT/"data/research"
    lists, originals = [], []
    for pattern in ("kr-net-alpha-rights-census-*-v1.json", "kr-net-alpha-kind-*-census-v1.json",
                    "kr-net-alpha-kind-effective-*-v1.json"):
        for path in sorted(base.glob(pattern)):
            raw = path.read_bytes()
            files["observations/"+path.name] = raw
            report = json.loads(raw)
            if "rows" in report:
                lists.append({"file": path.name, "rows": len(report["rows"]), "state": report["state"]})
            else:
                originals.append(report["receipt"])
    for name in ("kr-net-alpha-kis-rev-split-035720-2021-04-v1.zip",):
        files["prior/"+name] = (base/name).read_bytes()
    for day in ("2021-04-09", "2021-04-12", "2021-04-13", "2021-04-14", "2021-04-15"):
        files["prior/prices-"+day+".zip"] = (DEST/"stk_bydd_trd"/(day+".zip")).read_bytes()
    size = sum(map(len, files.values()))
    if source_storage_bytes()+size > collector.spec["max_new_archive_bytes"]:
        raise ValueError("snapshot exceeds source byte cap")
    manifest = {"schema": "net-alpha-effective-source-investigation-v1",
        "candidate_id": collector.spec["candidate_id"], "contract_hashes": collector.spec["contract_hashes"],
        "entries": {n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for n, b in files.items()},
        "source_lists": lists, "exact_original_receipts": sorted(originals),
        "missing_response_attempts": missing,
        "cumulative_requests": {**price_request_inventory(), "actions":
            collector.spec["amendment"]["source_scope"]["previous_action_requests"]+len(collector.journal)},
        "total_return_coverage": False, "strict_pit_eligible": False,
        "correction_scope_certified": False, "live_order_eligible": False}
    write_archive(output, files, manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--term", default="")
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite source audit")
    collector = Collector()
    if args.snapshot:
        report = snapshot(collector, args.output)
        print(json.dumps({"sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                          "originals": len(report["exact_original_receipts"]),
                          "cumulative_requests": report["cumulative_requests"]}))
        raise SystemExit(0)
    if not args.start or not args.end:
        parser.error("--start and --end required for a source census")
    raw, meta = collector.get(SEARCH+"?method=searchDetailsMain")
    form = _Form(); form.feed(raw.decode())
    report = census(collector, form.fields, args.start, args.end, args.max_pages, args.term)
    report.update(schema="net-alpha-action-list-audit-v1", form_sha256=meta["sha256"],
                  candidate_id=collector.spec["candidate_id"])
    sha = save(args.output, report)
    print(json.dumps({"sha256": sha, "rows": len(report["rows"]), "state": report["state"]}))
