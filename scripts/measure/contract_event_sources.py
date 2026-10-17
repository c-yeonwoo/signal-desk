"""Bounded official contract disclosures and historical minute source archives.

No order API or production store is accessed. Event selection uses filings only;
the price archive is a separate step after the event archive is frozen.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import math
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from signal_desk import market_clock

HOST = "https://kind.krx.co.kr"
SEARCH = HOST + "/disclosure/details.do"
START, END = "2026-05-01", "2026-07-31"
ZONE = ZoneInfo("Asia/Seoul")
MAX_EVENTS_PER_MONTH = 16


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode()


def _text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())


def _rows(source: str) -> list[list[str]]:
    return [[_text(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, re.S | re.I)]


class _Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside, self.fields = False, {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("id") == "searchForm"
        if (self.inside and tag == "input" and attrs.get("name")
                and (attrs.get("type") not in ("radio", "checkbox") or "checked" in attrs)):
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def parse_index(source: str) -> tuple[int, list[dict]]:
    total = re.search(r"전체\s*<em>([\d,]+)</em>", source)
    if not total:
        raise ValueError("KIND index total missing")
    events = []
    for raw in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, re.S | re.I):
        receipt = re.search(r"openDisclsViewer\('([0-9]{14})'", raw)
        if not receipt:
            continue
        code = re.search(r"companysummary_open\('([0-9]{5})'", raw)
        cells = [_text(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", raw, re.S | re.I)]
        if not code or len(cells) < 5:
            raise ValueError("KIND index issuer missing")
        published = dt.datetime.strptime(cells[1], "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)
        if (not START <= published.date().isoformat() <= END
                or not receipt[1].startswith(published.strftime("%Y%m%d"))):
            raise ValueError("KIND returned a filing outside the requested period")
        events.append({"receipt": receipt[1], "ticker": code[1] + "0", "issuer": cells[2],
                       "market_segment": "kospi" if re.search(r"alt=['\"]유가증권['\"]", raw)
                       else "kosdaq" if re.search(r"alt=['\"]코스닥['\"]", raw) else "other",
                       "title": cells[3], "published_at": published.isoformat(),
                       "publication_precision": "minute", "session": published.date().isoformat()})
    return int(total[1].replace(",", "")), events


def parse_contract(source: str) -> dict:
    rows = _rows(source)
    def field(marker):
        matches = [row[-1] for row in rows if len(row) >= 2
                   and any(marker in re.sub(r"\s+", "", cell) for cell in row[:-1])]
        if len(matches) != 1:
            raise ValueError("contract field missing or ambiguous: " + marker)
        return matches[0]

    def number(marker):
        value = field(marker).replace(",", "").strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", value):
            raise ValueError("contract numeric unit ambiguous")
        return float(value)

    amount, sales, ratio = (number(marker) for marker in (
        "계약금액(원)", "최근매출액(원)", "매출액대비(%)"))
    if amount <= 0 or sales <= 0 or ratio <= 0 or not all(math.isfinite(v) for v in (amount, sales, ratio)):
        raise ValueError("nonpositive contract amount")
    if abs(amount / sales * 100 - ratio) > 0.020000001:
        raise ValueError("amount/revenue and reported ratio differ")
    counterparty = field("계약상대")
    if not counterparty or counterparty in ("-", "없음"):
        raise ValueError("contract counterparty unspecified")
    related = field("관련공시")
    if related not in ("-", "없음", "해당사항없음", "해당사항 없음"):
        raise ValueError("earlier related disclosure exists")
    return {"amount_krw": amount, "recent_revenue_krw": sales,
            "revenue_ratio_pct": ratio, "counterparty": counterparty,
            "amount_basis": "filing_issuer_direct_contract", "issuer_role": "seller_or_supplier"}


def original_document(shell: str, event: dict) -> str:
    # KIND document numbers can predate public submission (a prepared document).
    # The index publication clock and the option's displayed filing date bind it;
    # a numeric document ID is never treated as the public timestamp.
    options = re.findall(r"<option\s+value=['\"]([0-9]{14})\|[YN]['\"][^>]*>(.*?)</option>", shell, re.S)
    display_date = event["session"].replace("-", ".")
    matches = [doc for doc, label in options if display_date in _text(label)
               and "체결" in _text(label)
               and not any(marker in _text(label) for marker in ("정정", "해지"))]
    if len(matches) != 1:
        raise ValueError("original filing document missing or ambiguous")
    return matches[0]


def _save(path: Path, files: dict[str, bytes], manifest: dict) -> None:
    manifest = {**manifest, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "entries": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                            for name, raw in files.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "x", ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
        archive.writestr("manifest.json", _json(manifest))


def load_archive(path: Path, schema: str) -> tuple[dict, dict[str, bytes]]:
    files = {}
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != schema:
            raise ValueError("unexpected event source schema")
        entries = manifest.get("entries") or {}
        if set(archive.namelist()) != set(entries) | {"manifest.json"}:
            raise ValueError("archive member mismatch")
        for name, entry in entries.items():
            raw = archive.read(name)
            if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("event source hash mismatch")
            files[name] = raw
    return manifest, files


def collect_events(output: Path) -> dict:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    files, requests = {}, 0

    def get(url, name, data=None):
        nonlocal requests
        requests += 1
        if requests > 600:
            raise ValueError("contract source request budget exceeded")
        req = urllib.request.Request(url, data, headers={"Referer": SEARCH,
                                                        "User-Agent": "signal-desk-research/1.0"})
        with opener.open(req, timeout=20) as response:
            if urllib.parse.urlparse(response.geturl()).hostname != "kind.krx.co.kr":
                raise ValueError("unexpected source redirect")
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("KIND source too large")
        files[name] = raw
        time.sleep(0.1)
        return raw.decode("utf-8")

    form = _Form()
    form.feed(get(SEARCH + "?method=searchDetailsMain", "raw/search-form.htm"))
    fields = {**form.fields, "method": "searchDetailsSub", "forward": "details_sub",
              "currentPageSize": "100", "fromDate": START, "toDate": END,
              "reportNm": "단일판매", "reportNmTemp": "단일판매", "marketType": "",
              "orderMode": "0", "orderStat": "D"}
    events, expected = [], None
    for page in range(1, 21):
        source = get(SEARCH, f"raw/index-{page}.htm", urllib.parse.urlencode(
            {**fields, "pageIndex": str(page)}).encode())
        total, rows = parse_index(source)
        expected = total if expected is None else expected
        if total != expected or total > 2000:
            raise ValueError("KIND pagination count changed or exceeds cap")
        events.extend(rows)
        if len(events) >= expected:
            break
    if len(events) != expected or len({row["receipt"] for row in events}) != expected:
        raise ValueError("KIND incomplete or duplicate filing census")
    selected, ledger, chosen_days, months = [], [], set(), Counter()
    for event in sorted(events, key=lambda row: (row["published_at"], row["receipt"])):
        title, session = event["title"], event["session"]
        clock = event["published_at"][11:16]
        if event["market_segment"] not in ("kospi", "kosdaq"):
            reason = "outside_kospi_kosdaq"
        elif any(marker in title for marker in ("정정", "해지", "자회사")) or "체결" not in title:
            reason = "not_initial_direct_contract"
        elif not market_clock.is_session("kr", session):
            reason = "not_regular_market_session"
        elif not "09:15" <= clock <= "13:30":
            reason = "outside_fixed_time_window"
        elif session in chosen_days:
            reason = "later_same_day_event"
        elif months[session[:7]] >= MAX_EVENTS_PER_MONTH:
            reason = "monthly_sample_cap"
        else:
            receipt = event["receipt"]
            try:
                shell = get(HOST + "/common/disclsviewer.do?method=search&acptno=" + receipt,
                            f"raw/{receipt}-viewer.htm")
                expected_title = f"[{event['issuer']}]"
                if expected_title not in shell:
                    raise ValueError("viewer issuer does not match index issuer")
                doc = original_document(shell, event)
                resolver = get(HOST + "/common/disclsviewer.do?method=searchContents&docNo=" + doc,
                               f"raw/{receipt}-resolver.htm")
                urls = re.findall(r"https://kind\.krx\.co\.kr/external/[0-9/]+/[0-9]+\.htm", resolver)
                if len(set(urls)) != 1 or "/" + doc + "/" not in urls[0]:
                    raise ValueError("original content path not resolved")
                body = get(urls[0], f"raw/{receipt}-original.htm")
                facts = parse_contract(body)
                if facts["revenue_ratio_pct"] < 10:
                    reason = "below_fixed_materiality"
                else:
                    selected.append({**event, **facts, "original_url": urls[0],
                                     "source_file": f"raw/{receipt}-original.htm",
                                     "source_sha256": hashlib.sha256(files[f"raw/{receipt}-original.htm"]).hexdigest(),
                                     "cohort": "development" if session[5:7] == "05" else "validation"})
                    chosen_days.add(session)
                    months[session[:7]] += 1
                    reason = "selected_before_prices"
            except (ValueError, urllib.error.URLError, TimeoutError) as exc:
                reason = "source_review_failed:" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        ledger.append({**event, "selection_reason": reason})
    manifest = {"schema": "contract-event-filings-v1", "events": selected, "ledger": ledger,
                "request_count": requests, "census_count": expected, "selected_by_month": dict(months),
                "period": [START, END], "strict_pit_eligible": False, "live_order_eligible": False}
    _save(output, files, manifest)
    return {key: manifest[key] for key in ("request_count", "census_count", "selected_by_month")}


def collect_minutes(event_archive: Path, output: Path) -> dict:
    from signal_desk import config
    from signal_desk.broker import kis
    manifest, _ = load_archive(event_archive, "contract-event-filings-v1")
    config.load_env()
    credentials = config.kis_credentials()
    if not credentials:
        raise ValueError("KIS read credentials are missing")
    credentials = {**credentials, "env": "real"}
    files, observations = {}, []
    for event in manifest["events"]:
        stamp = dt.datetime.fromisoformat(event["published_at"])
        end = (stamp + dt.timedelta(minutes=90)).strftime("%H%M%S")
        for ticker in (event["ticker"], "069500"):
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                      "FID_INPUT_HOUR_1": end, "FID_INPUT_DATE_1": stamp.strftime("%Y%m%d"),
                      "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""}
            body = kis._request("/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                                "FHKST03010230", credentials, params)
            name = f"raw/{event['receipt']}-{ticker}.json"
            files[name] = _json(body)
            observations.append({"receipt": event["receipt"], "ticker": ticker,
                                 "request": params, "source_file": name,
                                 "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                 "status": "observed" if isinstance(body, dict) and body.get("rt_cd") == "0" else "provider_failed"})
            time.sleep(0.3)
    _save(output, files, {"schema": "contract-event-minutes-v1", "observations": observations,
                          "event_archive_sha256": hashlib.sha256(event_archive.read_bytes()).hexdigest(),
                          "request_count": len(observations), "source_time_verified": False,
                          "strict_pit_eligible": False, "live_order_eligible": False})
    return {"request_count": len(observations), "statuses": dict(Counter(row["status"] for row in observations))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("filings", "minutes"))
    parser.add_argument("--event-archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("existing source archives cannot be overwritten")
    if args.phase == "minutes" and not args.event_archive:
        parser.error("a frozen event archive is required")
    result = collect_events(args.output) if args.phase == "filings" else collect_minutes(args.event_archive, args.output)
    print(json.dumps({"output": str(args.output), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
