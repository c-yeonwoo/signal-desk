"""Bounded KIND listing originals for observed R3 execution gaps, never outcomes."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from scripts.measure.contract_event_sources import HOST, SEARCH, _Form, _save, _text, load_archive
from scripts.measure.earnings_event_sources import parse_listing
from signal_desk.signals.research_action_coverage import ReviewedShareCountChange

SOURCE_SHA = "becd863bc6b9c9417922fd735736ea9dc95e2355c15da2c603f22606e5340f59"
# Identity and legal names read from the preserved completed listing notices.
IDENTITIES = {"395400": ("KR7395400005", "에스케이위탁관리부동산투자회사"),
              "323410": ("KR7323410001", "카카오뱅크"), "068270": ("KR7068270008", "셀트리온"),
              "055550": ("KR7055550008", "신한금융지주회사"), "000270": ("KR7000270009", "기아"),
              "042700": ("KR7042700005", "한미반도체"), "377300": ("KR7377300009", "카카오페이"),
              "035720": ("KR7035720002", "카카오")}


def parse_fact(source, entry, priority, observed):
    """Exact identity, amounts, effective dates and listing date; no rights/splits."""
    ticker = entry["ticker"]
    isin, issuer = IDENTITIES[ticker]
    text = _text(source.decode())
    codes = re.findall(r"(KR[0-9A-Z]{10})\s*\(단축코드:A" + ticker + r"\)", text)
    if (issuer not in text or codes != [isin]
            or "보통주" not in text):
        raise ValueError("listing security, issuer or class differs")
    before, after = priority["listed_shares_before"], priority["listed_shares_after"]
    if "변경상장(주식소각)" in text and "변경사유 : 주식소각" in text:
        kind = "treasury_cancellation"
        counts = re.findall(r"기명식 보통주 ([\d,]+)주\s*→\s*([\d,]+)", text)
        delta = re.findall(r"변경주식수\s*:\s*-([\d,]+)주", text)
        if (len(counts) != 1 or tuple(int(v.replace(",", "")) for v in counts[0]) != (before, after)
                or len(delta) != 1 or int(delta[0].replace(",", "")) != before - after):
            raise ValueError("cancellation amounts differ from KRX")
        effective_part = text.split("④ 발행일", 1)[1].split("⑤", 1)[0]
        listing_part = text.split("변경상장일 :", 1)[1].split("⑧", 1)[0]
    elif "추가상장" in text and ("증자방법 : 주식매수선택권 행사" in text or "증자방법 : 국내CB전환" in text):
        method = text.split("⑤ 증자방법 :", 1)[1].split("⑥", 1)[0].strip()
        if method not in ("주식매수선택권 행사", "국내CB전환"):
            raise ValueError("mixed or ambiguous issuance method")
        kind = "option_exercise_issuance" if method == "주식매수선택권 행사" else "convertible_bond_issuance"
        amount_part = text.split("① 주식의 종류와 수", 1)[1].split("②", 1)[0]
        if "우선주" in amount_part:
            raise ValueError("mixed security classes")
        total = re.match(r"\s*:\s*(?:기명식 보통주 )?([\d,]+)주", amount_part)
        components = re.findall(r"기명식 보통주 ([\d,]+)주", amount_part)
        amount = int(total[1].replace(",", "")) if total else sum(int(v.replace(",", "")) for v in components)
        if after <= before or amount != after - before or (components and sum(int(v.replace(",", "")) for v in components) != amount):
            raise ValueError("issuance amount differs from KRX")
        effective_part = text.split("③ 발행일", 1)[1].split("④", 1)[0]
        listing_part = text.split("⑥ 상장일 :", 1)[1].split("⑦", 1)[0]
        if kind == "convertible_bond_issuance":
            batches = re.findall(r"\(([\d,]+)주\)", effective_part)
            if not batches or sum(int(v.replace(",", "")) for v in batches) != amount:
                raise ValueError("conversion batches differ from issuance total")
    else:
        raise ValueError("unsupported listing type")

    def dates(part):
        return [dt.date(*(int(v) for v in match)).isoformat()
                for match in re.findall(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", part)]

    effective, listing = dates(effective_part), dates(listing_part)
    if (not effective or len(listing) != 1 or listing[0] != entry["krx_change_session"]
            or max(effective) > listing[0]):
        raise ValueError("issuance/cancellation/listing dates differ")
    return ReviewedShareCountChange(ticker=ticker, security_id=isin, share_class="ordinary_common", kind=kind,
        # Aggregate CB batches: first issuance date; the full dated batch span is retained below.
        effective_on=min(effective), listing_session=listing[0], previous_session=priority["previous_session"],
        listed_shares_before=before, listed_shares_after=after, source_url=entry["original_url"],
        source_sha256=hashlib.sha256(source).hexdigest(), source_filing_date=entry["session"],
        source_observed_at=dt.datetime.fromisoformat(observed),
        source_span=f"{issuer}; {isin}; {amount_part if kind != 'treasury_cancellation' else counts}; "
                    f"first_effective_date_of_listed_batch; {effective_part}; {listing_part}")


def reviewed_facts(path, by_day, panel):
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError("pinned R3 listing archive differs")
    manifest, files = load_archive(path, "r3-listing-originals-v1")
    facts = {}
    for entry in manifest["ledger"]:
        day, ticker = entry["krx_change_session"], entry["ticker"]
        previous = panel.sessions[panel.index[day] - 1]
        priority = {"previous_session": previous,
                    "listed_shares_before": int(by_day[previous][ticker]["LIST_SHRS"].replace(",", "")),
                    "listed_shares_after": int(by_day[day][ticker]["LIST_SHRS"].replace(",", ""))}
        fact = parse_fact(files[entry["source_file"]], entry, priority, manifest["observed_at"])
        if (day, ticker) in facts:
            raise ValueError("ambiguous listing for one KRX change")
        facts[day, ticker] = fact
    if set(facts) != {tuple(row) for row in manifest["queue"]}:
        raise ValueError("incomplete fixed listing queue")
    return facts


def collect(result_path, output):
    first = json.loads(result_path.read_text())
    if (first.get("schema") != "kr-ridge-selection-result-v1"
            or first.get("candidate_id") != "kr-continuous-ridge-h5-v1"):
        raise ValueError("not the frozen R3 experiment")
    queue = sorted({(r["session"], r["ticker"]) for r in first["verdict"]["gaps"]
                    if r.get("session") and r.get("ticker")})
    if (not queue or len(queue) > 20 or any(not "2024-10-02" <= day <= "2025-01-31"
            or not re.fullmatch(r"[0-9]{6}", ticker) for day, ticker in queue)):
        raise ValueError("source queue outside bounded R3 dates or size")
    files, ledger, requests = {}, [], 0

    def get(url, name, data=None):
        nonlocal requests
        if name in files:
            return files[name].decode()
        if requests >= 100:
            raise ValueError("bounded listing source budget exhausted")
        requests += 1
        request = urllib.request.Request(url, data, headers={"Referer": SEARCH,
                                        "User-Agent": "signal-desk-research/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            if urllib.parse.urlparse(response.geturl()).hostname != "kind.krx.co.kr":
                raise ValueError("unexpected official source redirect")
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("oversized listing notice")
        files[name] = raw
        time.sleep(.15)
        return raw.decode()

    form = _Form()
    form.feed(get(SEARCH + "?method=searchDetailsMain", "raw/form.htm"))
    for day, ticker in queue:
        start = (dt.date.fromisoformat(day) - dt.timedelta(days=14)).isoformat()
        gap_states = {r["state"] for r in first["verdict"]["gaps"]
                      if r.get("session") == day and r.get("ticker") == ticker}
        terms = ("매매거래정지",) if "incomplete_exit_open" in gap_states else ("추가상장", "변경상장")
        for term in terms:
            found, expected = [], None
            for page in range(1, 6):
                fields = {**form.fields, "method": "searchDetailsSub", "forward": "details_sub",
                          "currentPageSize": "100", "fromDate": start, "toDate": day,
                          "reportNm": term, "reportNmTemp": term, "marketType": "1",
                          "orderMode": "0", "orderStat": "D", "pageIndex": str(page)}
                total, batch = parse_listing(get(SEARCH, f"raw/{day}-{term}-{page}.htm",
                    urllib.parse.urlencode(fields).encode()), start=start, end=day)
                expected = total if expected is None else expected
                if total != expected or total > 500:
                    raise ValueError("listing census changed or exceeded budget")
                found.extend(batch)
                if len(found) >= expected:
                    break
            if len(found) != expected or len({r["receipt"] for r in found}) != expected:
                raise ValueError("listing census incomplete")
            for event in found:
                if event["ticker"] != ticker:
                    continue
                receipt = event["receipt"]
                shell = get(HOST + "/common/disclsviewer.do?method=search&acptno=" + receipt,
                            f"raw/{receipt}-viewer.htm")
                options = re.findall(r"<option\s+value=['\"]([0-9]{14})\|[YN]['\"][^>]*>(.*?)</option>", shell, re.S)
                docs = [doc for doc, label in options if event["session"].replace("-", ".") in _text(label)
                        and term in _text(label) and ("정정" in event["title"]) == ("정정" in _text(label))]
                if len(docs) != 1 or f"[{event['issuer']}]" not in shell:
                    raise ValueError("listing version or issuer ambiguous")
                resolver = get(HOST + "/common/disclsviewer.do?method=searchContents&docNo=" + docs[0],
                               f"raw/{receipt}-resolver.htm")
                urls = re.findall(r"https://kind\.krx\.co\.kr/external/[0-9/]+/[A-Za-z0-9]+\.htm", resolver)
                if len(set(urls)) != 1 or "/" + docs[0] + "/" not in urls[0]:
                    raise ValueError("listing original unresolved")
                name = f"raw/{receipt}-original.htm"
                get(urls[0], name)
                ledger.append({**event, "krx_change_session": day, "original_url": urls[0],
                    "source_file": name, "source_sha256": hashlib.sha256(files[name]).hexdigest()})
    manifest = {"schema": "r3-listing-originals-v1", "gap_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "queue": queue, "ledger": ledger, "request_count": requests,
                "live_order_eligible": False, "coverage_state": "partial"}
    _save(output, files, manifest)
    return {"requests": requests, "queue": queue, "ledger": ledger}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite originals")
    print(json.dumps(collect(args.result, args.output), ensure_ascii=False))
