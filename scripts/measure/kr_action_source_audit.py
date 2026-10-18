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
import xml.etree.ElementTree as ET

from scripts.measure.kr_net_alpha_sources import ROOT, DEST, _NoRedirect
from scripts.measure.kr_net_alpha_v2 import contract, source_storage_bytes, price_request_inventory, JOURNAL_SHA
from scripts.measure.acquire_historical_sources import _fetch_kis_schedule_page, write_archive
from scripts.measure.contract_event_sources import _Form, _text, SEARCH
from scripts.measure.kr_net_alpha_v2 import save
from signal_desk import config

HOSTS = frozenset({"kind.krx.co.kr", "seibro.or.kr", "www.seibro.or.kr", "api.seibro.or.kr",
                  "m.seibro.or.kr",
                  "global.krx.co.kr", "open.krx.co.kr", "law.krx.co.kr", "www.samsung.com", "www.data.go.kr",
                  "apis.data.go.kr"})
# Exact public documents only: adding a vendor must not authorize its paid API,
# authenticated downloads, account actions, or arbitrary same-host endpoints.
VENDOR_DOCUMENTS = frozenset({
    "https://databento.com/corporate-actions",
    "https://databento.com/blog/corporate-actions",
    "https://databento.com/docs/venues-and-datasets/corporate-actions",
    "https://databento.com/docs/schemas-and-data-formats/corporate-actions",
    "https://databento.com/docs/api-reference-reference",
    "https://sample-pcaps-dl.databento.com/reference/databento-corporate-actions-global-20200825.csv",
    "https://help-dataguide.fnguide.com/ko/articles/" +
    "%EC%9D%B4%EC%9A%A9-%EB%B0%8F-%EC%9A%94%EA%B8%88-%EC%95%88%EB%82%B4-48b18a4b",
    "https://developer.exchange-data.com/product/worldwide-corporate-actions",
    "https://developer.exchange-data.com/api-documentation/world_corporate_actions/GetHistoricalCorporateActions",
    "https://datamall.koscom.co.kr/kor/main.do",
    "https://developers.lseg.com/en/article-catalog/article/workspace-corporate-actions-content-set-guide",
})
FSC_RIGHTS = "https://apis.data.go.kr/1160100/GetStocRighScheService_V2/getRighExerReasSche_V2"
FSC_ISSUANCE = "https://apis.data.go.kr/1160100/GetStocIssuInfoService_V3/getStocIssuInfo_V3"
SEIBRO_CALENDAR = "https://seibro.or.kr/websquare/engine/proworks/callServletService.jsp"
SEIBRO_TASK = "ksd.safe.bip.cnts.Company.process.EntrSkedulPTask"
SEIBRO_ACTION = "termByImptSkedulList"
SEIBRO_DETAILS = {
    "divSkedulView": {"103"}, "issuDetailsList2": {"103"}, "payDetailsList1": {"103"},
    "faceDiviMergBasicInfoListEL1": {"201", "202"},
    "issuDetailsList3": {"201", "202"}, "payDetailsList2": {"201", "202"}}


def seibro_calendar_query(data, spec):
    """Only observed public calendar/detail reads, respecting UI range caps."""
    if not isinstance(data, bytes) or len(data) > 4096 or b"<!" in data:
        raise ValueError("unapproved SEIBro calendar XML")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("unapproved SEIBro calendar XML") from None
    action = root.get("action")
    names = ({"ISSUCO_CUSTNO", "RGT_STD_DT", "RGT_RACD"} if action in SEIBRO_DETAILS else
        {"ISSUCO_CUSTNO", "DT_TPCD", "FROMDATE", "TODATE", "LIST_TPCD", "RGT_RACD"})
    if (root.tag != "reqParam" or root.attrib != {"action":action, "task":SEIBRO_TASK}
            or action not in {SEIBRO_ACTION, *SEIBRO_DETAILS}
            or len(root) != len(names) or {n.tag for n in root} != names
            or any(set(n.attrib) != {"value"} or len(n) or (n.text or "").strip() for n in root)):
        raise ValueError("unapproved SEIBro calendar operation")
    params = {n.tag:n.attrib["value"] for n in root}
    if action in SEIBRO_DETAILS:
        try:
            date = dt.datetime.strptime(params["RGT_STD_DT"], "%Y%m%d").date()
        except ValueError:
            raise ValueError("invalid SEIBro detail date") from None
        if (date.strftime("%Y%m%d") != params["RGT_STD_DT"]
                or not spec["warmup_from"] <= date.isoformat() <= spec["liquidation_through"]
                or not re.fullmatch(r"[0-9]{1,8}", params["ISSUCO_CUSTNO"])
                or params["RGT_RACD"] not in SEIBRO_DETAILS[action]):
            raise ValueError("SEIBro detail query outside public research bounds")
        return {"action":action, **params}
    try:
        dates = [dt.datetime.strptime(params[k], "%Y%m%d").date() for k in ("FROMDATE", "TODATE")]
    except ValueError:
        raise ValueError("invalid SEIBro calendar range") from None
    issuer = params["ISSUCO_CUSTNO"]
    if (any(d.strftime("%Y%m%d") != params[k] for d,k in zip(dates, ("FROMDATE", "TODATE")))
            or not spec["warmup_from"] <= dates[0].isoformat() <= dates[1].isoformat() <= spec["liquidation_through"]
            or (dates[1]-dates[0]).days > (364 if issuer else 6)
            or (issuer and not re.fullmatch(r"[0-9]{1,8}", issuer))
            or params["LIST_TPCD"] != "11"
            or params["DT_TPCD"] not in {"", "01", "03", "07", "08", "09", "10", "12", "14", "24", "31"}
            or (params["RGT_RACD"] and not re.fullmatch(r"[0-9]{3}", params["RGT_RACD"]))):
        raise ValueError("SEIBro calendar query outside public research bounds")
    return params


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

    def get(self, url, data=None, *, _service_key=None, _refresh=False):
        parsed = urllib.parse.urlsplit(url)
        seibro_query = seibro_calendar_query(data, self.spec) if url == SEIBRO_CALENDAR and data is not None else None
        vendor_document = url in VENDOR_DOCUMENTS and data is None and _service_key is None
        if (parsed.scheme != "https" or (parsed.hostname not in HOSTS and not vendor_document) or parsed.username or parsed.password
                or any(word in k.lower() for k, _ in urllib.parse.parse_qsl(parsed.query)
                       for word in ("token", "secret", "servicekey", "auth_key"))
                or (data is not None and seibro_query is None and not (parsed.hostname == "kind.krx.co.kr"
                                             and parsed.path in ("/disclosure/details.do", "/disclosureinfo/dividendinfo.do")))):
            raise ValueError("URL outside read-only public-source scope")
        if _service_key is not None and (url.split("?", 1)[0] not in (FSC_RIGHTS, FSC_ISSUANCE) or data is not None):
            raise ValueError("service key destination outside approved FSC operation")
        if _refresh and _service_key is None:
            raise ValueError("explicit recheck only supported for approved FSC authentication")
        key_material = url.encode()+b"\0"+(data or b"")
        if _service_key is not None:
            if not _service_key:
                raise ValueError("empty service key")
            key_material += b"\0"+_service_key.encode()
        key = hashlib.sha256(key_material).hexdigest()
        for row in (() if _refresh else reversed(self.journal)):
            if row.get("request_key") == key and row.get("response_path"):
                path = (DEST/row["response_path"]).resolve()
                if not path.is_relative_to(DEST.resolve()):
                    raise ValueError("response path outside research directory")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                    raise ValueError("saved source response differs")
                if row["state"] != "response_saved" and _service_key is None:
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
        if _service_key is not None:
            row["authentication_variable"] = "DATA_GO_KR_SERVICE_KEY"
        if seibro_query is not None:
            row.update(request_xml_sha256=hashlib.sha256(data).hexdigest(), calendar_query=seibro_query)
        elif data is not None:
            row["request_form"] = urllib.parse.parse_qs(data.decode(), keep_blank_values=True)
        self.journal.append(row); self.save(); self.previous = time.monotonic()
        try:
            request_url = url if _service_key is None else url+("&" if parsed.query else "?")+urllib.parse.urlencode({"serviceKey": _service_key})
            headers = {"User-Agent": "signal-desk-research/1.0", "Referer": f"https://{parsed.hostname}/"}
            if seibro_query is not None:
                headers["Content-Type"] = "application/xml; charset=UTF-8"
            req = urllib.request.Request(request_url, data, headers=headers)
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                response = opener.open(req, timeout=45 if _service_key is not None else 20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(5_000_001)
                row["http_status"] = response.code
                row["content_type"] = response.headers.get("Content-Type", "")
                if response.headers.get("Location"):
                    row["location"] = response.headers["Location"] if _service_key is None else "redacted_authenticated_redirect"
            if len(raw) > 5_000_000 or source_storage_bytes()+len(raw) > self.spec["max_new_archive_bytes"]:
                raise ValueError("source byte cap reached")
            if _service_key is not None and any(secret.encode() in raw for secret in (
                    _service_key, urllib.parse.quote(_service_key, safe=""))):
                row.update(state="credential_echo_not_saved", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                raise ValueError("credential echoed by provider; response not saved")
            path = DEST/"effective-source-responses"/f"{len(self.journal):04d}.bin"
            path.parent.mkdir(exist_ok=True)
            if path.exists():
                raise ValueError("refusing to overwrite source bytes")
            path.write_bytes(raw)
            row.update(state="response_saved" if 200 <= row["http_status"] < 300 else "http_error",
                       bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                       response_path=str(path.relative_to(DEST)))
            if row["state"] != "response_saved" and _service_key is None:
                raise ValueError(f"source HTTP {row['http_status']}")
            return raw, row
        except BaseException as exc:
            if row["state"] == "reserved": row.update(state="failed", error=type(exc).__name__)
            raise
        finally:
            self.save()

    def fsc_rights(self, day, page=1, page_size=100, issuer=None):
        return self._fsc_schedule(FSC_RIGHTS, day, page, page_size, issuer)

    def seibro_calendar(self, start, end, *, date_role="03", reason="", issuer=""):
        root = ET.Element("reqParam", {"action":SEIBRO_ACTION, "task":SEIBRO_TASK})
        for key,value in {"ISSUCO_CUSTNO":issuer, "DT_TPCD":date_role,
                "FROMDATE":dt.date.fromisoformat(start).strftime("%Y%m%d"),
                "TODATE":dt.date.fromisoformat(end).strftime("%Y%m%d"),
                "LIST_TPCD":"11", "RGT_RACD":reason}.items():
            ET.SubElement(root, key, {"value":value})
        return self.get(SEIBRO_CALENDAR, ET.tostring(root, encoding="utf-8"))

    def seibro_detail(self, action, issuer, record_on, reason):
        root = ET.Element("reqParam", {"action":action, "task":SEIBRO_TASK})
        for key,value in {"ISSUCO_CUSTNO":issuer, "RGT_STD_DT":dt.date.fromisoformat(record_on).strftime("%Y%m%d"),
                          "RGT_RACD":reason}.items():
            ET.SubElement(root, key, {"value":value})
        return self.get(SEIBRO_CALENDAR, ET.tostring(root, encoding="utf-8"))

    def fsc_issuance(self, day, page=1, page_size=100, issuer=None, *, recheck=False):
        return self._fsc_schedule(FSC_ISSUANCE, day, page, page_size, issuer, recheck=recheck)

    def _fsc_schedule(self, endpoint, day, page, page_size, issuer, *, recheck=False):
        """One official operation; credentials never enter source journals."""
        if (dt.date.fromisoformat(day).isoformat() != day or
                not self.spec["warmup_from"] <= day <= self.spec["liquidation_through"]
                or type(page) is not int or type(page_size) is not int
                or not 1 <= page <= 30 or not 1 <= page_size <= 100
                or (issuer is not None and (not isinstance(issuer, str) or not 1 <= len(issuer) <= 80))):
            raise ValueError("FSC query outside fixed research scope")
        import os
        config.load_env()
        secret = urllib.parse.unquote(os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip())
        if not secret:
            raise ValueError("DATA_GO_KR_SERVICE_KEY missing")
        params = {"basDt": day.replace("-", ""), "pageNo": page, "numOfRows": page_size, "resultType": "json"}
        if issuer is not None:
            params["stckIssuCmpyNm"] = issuer
        try:
            return self.get(endpoint+"?"+urllib.parse.urlencode(params), _service_key=secret, _refresh=recheck)
        except Exception as exc:
            raise ValueError(f"FSC source request failed ({type(exc).__name__}); see redacted journal") from None


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
            or not 1 <= max_pages <= 30 or term not in ("", "주식분할", "배당", "변경상장", "추가상장", "재상장", "매매거래정지")):
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
