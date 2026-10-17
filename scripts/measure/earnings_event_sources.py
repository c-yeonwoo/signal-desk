"""Bounded official earnings/guidance originals, without prices or order calls."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.cookiejar
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from scripts.measure.contract_event_sources import HOST, SEARCH, ZONE, _Form, _save, _text, load_archive

START, END = "2026-01-01", "2026-07-31"
TERMS = {"guidance": "영업실적등에대한전망", "earnings": "영업(잠정)실적"}
PERIODS = {"baseline_guidance": ("2025-01-01", "2025-12-31"),
           "guidance": (START, END), "earnings": (START, END)}


def parse_listing(source: str, *, start: str = START, end: str = END) -> tuple[int, list[dict]]:
    total = re.search(r"전체\s*<em>([\d,]+)</em>", source)
    if not total:
        raise ValueError("filing census missing")
    rows = []
    for raw in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, re.S | re.I):
        receipt = re.search(r"openDisclsViewer\('([0-9]{14})'", raw)
        if not receipt:
            continue
        code = re.search(r"companysummary_open\('([0-9A-Z]{5})'", raw)
        cells = [_text(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", raw, re.S | re.I)]
        if not code or len(cells) < 5:
            raise ValueError("filing identity missing")
        stamp = dt.datetime.strptime(cells[1], "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)
        if not start <= stamp.date().isoformat() <= end or not receipt[1].startswith(stamp.strftime("%Y%m%d")):
            raise ValueError("filing outside fixed period")
        rows.append({"receipt": receipt[1], "ticker": code[1] + "0", "issuer": cells[2],
                     "market_segment": "kospi" if re.search(r"alt=['\"]유가증권['\"]", raw)
                     else "kosdaq" if re.search(r"alt=['\"]코스닥['\"]", raw) else "other",
                     "title": cells[3], "published_at": stamp.isoformat(),
                     "session": stamp.date().isoformat()})
    return int(total[1].replace(",", "")), rows


def document_id(shell: str, event: dict) -> str:
    options = re.findall(r"<option\s+value=['\"]([0-9]{14})\|[YN]['\"][^>]*>(.*?)</option>", shell, re.S)
    correction = "정정" in event["title"]
    date = event["session"].replace("-", ".")
    matches = [doc for doc, label in options if date in _text(label)
               and ("정정" in _text(label)) == correction
               and ("전망" in _text(label)) == (event["kind"] == "guidance")
               and ("전망" in _text(label) or "실적" in _text(label))]
    if len(matches) != 1:
        raise ValueError("as-published document version ambiguous")
    return matches[0]


def collect(output: Path, reuse: Path | None = None) -> dict:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    files, requests, reused = {}, 0, 0
    cached = load_archive(reuse, "earnings-guidance-originals-v1")[1] if reuse else {}

    def get(url, name, data=None):
        nonlocal requests, reused
        if name in cached and "/index-" not in name and name != "raw/form.htm":
            files[name] = cached[name]
            reused += 1
            return cached[name].decode()
        if requests >= 600:
            raise RuntimeError("fixed filing request budget exhausted")
        requests += 1
        req = urllib.request.Request(url, data, headers={"Referer": SEARCH,
                                                        "User-Agent": "signal-desk-research/1.0"})
        with opener.open(req, timeout=20) as response:
            if urllib.parse.urlparse(response.geturl()).hostname != "kind.krx.co.kr":
                raise ValueError("unexpected source redirect")
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("oversized filing source")
        files[name] = raw
        time.sleep(.15)
        return raw.decode()

    form = _Form()
    form.feed(get(SEARCH + "?method=searchDetailsMain", "raw/form.htm"))
    census, counts = [], {}
    for group, (date_start, date_end) in PERIODS.items():
        kind = "guidance" if group == "baseline_guidance" else group
        term = TERMS[kind]
        rows, expected = [], None
        for page in range(1, 26):
            fields = {**form.fields, "method": "searchDetailsSub", "forward": "details_sub",
                      "currentPageSize": "100", "fromDate": date_start, "toDate": date_end,
                      "reportNm": term, "reportNmTemp": term, "marketType": "",
                      "orderMode": "0", "orderStat": "D", "pageIndex": str(page)}
            total, batch = parse_listing(get(SEARCH, f"raw/{group}/index-{page}.htm",
                                             urllib.parse.urlencode(fields).encode()), start=date_start, end=date_end)
            expected = total if expected is None else expected
            if total != expected or total > 2500:
                raise ValueError("filing census changed or too large")
            rows.extend({**row, "kind": kind, "source_group": group} for row in batch)
            if len(rows) >= expected:
                break
        if len(rows) != expected or len({row["receipt"] for row in rows}) != expected:
            raise ValueError("filing census incomplete or duplicate")
        counts[group] = expected
        census.extend(rows)
    # Guidance companies, not their later price winners, define the earnings lookup.
    issuers = {row["ticker"] for row in census if row["source_group"] == "guidance"}
    ledger = []
    for event in sorted(census, key=lambda row: (row["published_at"], row["receipt"])):
        row = dict(event)
        if (event["market_segment"] not in ("kospi", "kosdaq")
                or not event["ticker"][0].isdigit() or "자회사" in event["title"]):
            row["status"] = "outside_direct_domestic_issuer"
        elif event["source_group"] in ("earnings", "baseline_guidance") and event["ticker"] not in issuers:
            row["status"] = "no_guidance_in_fixed_census"
        else:
            receipt = event["receipt"]
            try:
                shell = get(HOST + "/common/disclsviewer.do?method=search&acptno=" + receipt,
                            f"raw/{receipt}-viewer.htm")
                if f"[{event['issuer']}]" not in shell:
                    raise ValueError("viewer issuer differs")
                doc = document_id(shell, event)
                resolver = get(HOST + "/common/disclsviewer.do?method=searchContents&docNo=" + doc,
                               f"raw/{receipt}-resolver.htm")
                urls = re.findall(r"https://kind\.krx\.co\.kr/external/[0-9/]+/[0-9]+\.htm", resolver)
                if len(set(urls)) != 1 or "/" + doc + "/" not in urls[0]:
                    raise ValueError("original content unresolved")
                name = f"raw/{receipt}-original.htm"
                get(urls[0], name)
                row.update(status="original_preserved", original_url=urls[0], source_file=name,
                           source_sha256=hashlib.sha256(files[name]).hexdigest())
            except (ValueError, OSError, RuntimeError) as exc:
                row["status"] = "source_failed:" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        ledger.append(row)
    manifest = {"schema": "earnings-guidance-originals-v1", "period": [START, END],
                "census_counts": counts, "query_periods": PERIODS, "ledger": ledger, "request_count": requests,
                "reused_source_files": reused,
                "parent_sha256": hashlib.sha256(reuse.read_bytes()).hexdigest() if reuse else None,
                "strict_pit_eligible": False, "live_order_eligible": False}
    _save(output, files, manifest)
    return {"counts": counts, "request_count": requests,
            "statuses": dict(Counter(row["status"] for row in ledger))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-source", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("source archives cannot be overwritten")
    print(collect(args.output, args.reuse_source))


if __name__ == "__main__":
    main()
