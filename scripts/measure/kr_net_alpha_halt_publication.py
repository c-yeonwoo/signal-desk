"""Resolve the fixed Asiana halt's publication time, not an action-free census."""

import argparse
import datetime as dt
import fcntl
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import verify_action_archive, write_archive
from scripts.measure.contract_event_sources import HOST, SEARCH, _Form
from scripts.measure.earnings_event_sources import parse_listing
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest
from scripts.measure.kr_net_alpha_v2 import contract, save, source_storage_bytes

ORIGINAL = ROOT/"data/research/kr-net-alpha-kind-asiana-halt-2021-v1.zip"
ORIGINAL_SHA = "2b99b665479f407987f1981d00de6ff9960cb0324a1c19e4d3cc8b5c602ce193"
DAY, RECEIPT, ISIN = "2021-05-26", "20210526000293", "KR7020560009"


def collect(output, packet):
    spec = contract()
    output, packet = output.resolve(), packet.resolve()
    if packet.exists():
        raise ValueError("refusing to overwrite halt evidence")
    if digest(ORIGINAL.read_bytes()) != ORIGINAL_SHA:
        raise ValueError("pinned halt original differs")
    verify_action_archive(ORIGINAL)
    lock = (DEST/".collector.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    journal_path = DEST/"v2-action-requests.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else []
    files, previous, new_requests = {}, 0.0, 0

    def get(name, data=None):
        nonlocal previous, new_requests
        for cached in reversed(journal):
            if cached.get("member") == name and cached.get("response_path"):
                target = (DEST/cached["response_path"]).resolve()
                if not target.is_relative_to(DEST.resolve()):
                    raise ValueError("cached response path outside journal")
                raw = target.read_bytes()
                if digest(raw) != cached["sha256"]:
                    raise ValueError("cached halt response differs")
                files[name] = raw
                return raw.decode()
        if spec["amendment"]["source_scope"]["previous_action_requests"]+len(journal) >= spec["max_action_source_requests"]:
            raise ValueError("cumulative action request budget exhausted")
        if source_storage_bytes() >= spec["max_new_archive_bytes"]:
            raise ValueError("cumulative source byte cap reached")
        time.sleep(max(0, 1-(time.monotonic()-previous)))
        row = {"member": name, "state": "reserved",
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        journal.append(row)
        new_requests += 1
        journal_path.write_text(json.dumps(journal, indent=2))
        previous = time.monotonic()
        try:
            url = SEARCH if data else SEARCH+"?method=searchDetailsMain"
            req = urllib.request.Request(url, data, headers={"Referer": SEARCH,
                "User-Agent": "signal-desk-research/1.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                if urllib.parse.urlparse(response.geturl()).hostname != "kind.krx.co.kr":
                    raise ValueError("official source redirect differs")
                raw = response.read(5_000_001)
            if len(raw) > 5_000_000 or source_storage_bytes()+len(raw) > spec["max_new_archive_bytes"]:
                raise ValueError("oversized halt response")
            target = DEST/"v2-halt-responses"/f"{len(journal):04d}.bin"
            target.parent.mkdir(exist_ok=True)
            if target.exists():
                raise ValueError("halt response target already exists")
            target.write_bytes(raw)
            files[name] = raw
            row.update(state="response_saved", sha256=digest(raw), bytes=len(raw),
                       response_path=str(target.relative_to(DEST)))
            return raw.decode()
        except BaseException as exc:
            row.update(state="failed", error=type(exc).__name__)
            raise
        finally:
            journal_path.write_text(json.dumps(journal, indent=2))

    form = _Form()
    form.feed(get("raw/form.htm"))
    fields = {**form.fields, "method": "searchDetailsSub", "forward": "details_sub",
        "currentPageSize": "100", "fromDate": DAY, "toDate": DAY,
        "reportNm": "매매거래정지", "reportNmTemp": "매매거래정지", "marketType": "1",
        "orderMode": "0", "orderStat": "D", "pageIndex": "1"}
    total, rows = parse_listing(get("raw/index.htm", urllib.parse.urlencode(fields).encode()), start=DAY, end=DAY)
    matches = [r for r in rows if r["receipt"] == RECEIPT and r["ticker"] == "020560"]
    if len(matches) != 1:
        raise ValueError("fixed halt publication not found in first page; no speculative pagination")
    with ZipFile(ORIGINAL) as z:
        original_manifest = json.loads(z.read("manifest.json"))
        member = next(iter(original_manifest["entries"]))
        files["raw/original.htm"] = z.read(member)
    # Bind ISIN to the already preserved official date-specific roster.
    roster = json.loads((DEST/"universes.json").read_text())
    identity = [s for u in roster["dates"] if u["date"] == DAY
                for s in u["securities"] if s["ticker"] == "020560"]
    if len(identity) != 1 or identity[0]["isin"] != ISIN:
        raise ValueError("official historical Asiana identity differs")
    manifest = {"schema": "net-alpha-halt-publication-originals-v1", "request_count": new_requests,
        "cumulative_action_attempts": spec["amendment"]["source_scope"]["previous_action_requests"]+len(journal), "raw_listing_total": total,
        "unpreserved_response_attempts": [i for i, r in enumerate(journal, 1)
            if r.get("bytes") and not r.get("response_path")],
        "matched_filing": matches[0], "reused_original_archive_sha256": ORIGINAL_SHA,
        "entries": {name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in files.items()},
        "total_return_coverage": False, "strict_pit_eligible": False}
    if output.exists():
        with ZipFile(output) as z:
            existing = json.loads(z.read("manifest.json"))
            if (existing.get("schema") != manifest["schema"]
                    or existing.get("matched_filing") != matches[0]
                    or set(z.namelist()) != set(files)|{"manifest.json"}
                    or any(z.read(name) != raw for name, raw in files.items())):
                raise ValueError("existing halt archive differs")
    else:
        write_archive(output, files, manifest)
    fact = {"fact_id": RECEIPT, "event_id": RECEIPT, "security_id": ISIN, "kind": "halt",
        "effective_at": "2021-05-26T15:29:00+09:00", "published_on": DAY,
        "published_at": matches[0]["published_at"], "publication_precision": "minute",
        "source_sha256": digest(files["raw/original.htm"]),
        "publication_source_sha256": digest(files["raw/index.htm"]),
        "source_span": "2021년 05월 26일 15:29"}
    archive_sha = digest(output.read_bytes())
    payload = {"schema": "net-alpha-reviewed-evidence-v1", "documents": [
        {"path": str(output.relative_to(ROOT/"data/research")), "member": name,
         "archive_sha256": archive_sha, "sha256": digest(raw)}
        for name, raw in files.items()], "facts": [fact], "certificates": [],
        "identity_roster_sha256": digest((DEST/"universes.json").read_bytes())}
    sha = save(packet, payload)
    print(json.dumps({"fact": fact, "packet_sha256": sha, "archive_sha256": archive_sha,
                      "action_attempts": spec["amendment"]["source_scope"]["previous_action_requests"]+len(journal)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args()
    collect(args.output, args.packet)
