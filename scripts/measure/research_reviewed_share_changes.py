"""Bind seven selected KIND listing facts to the frozen KRX share-change queue.

Research only. A verified issuer event never certifies a complete action feed,
an account entitlement, or a historical trading signal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import verify_action_archive
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk.signals.research_action_coverage import ReviewedShareCountChange

PRIORITY = ROOT / "docs/research-action-holding-change-priority-2026-10-12.json"
PRIORITY_SHA256 = "1d1b75dca321215ac0be4dbb4a076dff1d533bc5d87168efb3933246cc9541f4"
SECURITY_IDS = {"000660": "KR7000660001", "005930": "KR7005930003",
                "035720": "KR7035720002", "105560": "KR7105560007",
                "402340": "KR7402340004"}

# Only completed listing notices are economic-fact inputs. Quarterly reports
# and treasury-acquisition decisions can corroborate them but cannot replace one.
SOURCES = (
    ("kind-held-change-followup-2026-10-12-v1.zip",
     "29c95e11e733a5cb67e76eca505e16b38cfb0077959c315f04700b6cb6f9a2ba",
     "skhynix-cancellation-listing-20260213", "000660"),
    ("kind-held-change-review-2026-10-12-v1.zip",
     "e343d6a41f869f1a23fc51f51759439255ff1ae568f10d3c92adc0e6540d0f2e",
     "samsung-cancellation-listing-20260409", "005930"),
    ("kind-held-change-review-2026-10-12-v1.zip",
     "e343d6a41f869f1a23fc51f51759439255ff1ae568f10d3c92adc0e6540d0f2e",
     "kakao-option-listing-20251111", "035720"),
    ("kind-final-held-change-2026-10-12-v1.zip",
     "4ee0bb01484224b74b97432fe8b1a47b2dd976a59f9f67795b07155c72fbfb79",
     "kb-cancellation-listing-20260202", "105560"),
    ("kind-final-held-change-2026-10-12-v1.zip",
     "4ee0bb01484224b74b97432fe8b1a47b2dd976a59f9f67795b07155c72fbfb79",
     "kb-cancellation-listing-20260604", "105560"),
    ("kind-sksquare-change-2026-10-12-v1.zip",
     "d4a91cb60d52f9493446b76db5a1650bab5fab0deeac1c5cd43c5eb38561d5c1",
     "sksquare-cancellation-listing-20251212", "402340"),
    ("kind-final-held-change-2026-10-12-v1.zip",
     "4ee0bb01484224b74b97432fe8b1a47b2dd976a59f9f67795b07155c72fbfb79",
     "sksquare-cancellation-listing-20260413", "402340"),
)

_CANCELLATION = re.compile(
    r"기명식\s+보통주\s+([\d,]+)주\s*→\s*([\d,]+)주")
_ISSUANCE = re.compile(r"주식의\s+종류와\s+수\s*:\s*기명식\s+보통주\s+([\d,]+)주")
_CODE = re.compile(r"(KR[0-9A-Z]{10})\s*\(단축코드:A([0-9A-Z]{6})\)")
_EFFECTIVE_CANCEL = re.compile(r"발행일\(소각일\)\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_EFFECTIVE_ISSUE = re.compile(r"③\s*발행일\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_LISTING_CANCEL = re.compile(r"변경상장일\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_LISTING_ISSUE = re.compile(r"⑥\s*상장일\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


def _one(pattern: re.Pattern[str], source: str, fact: str) -> re.Match[str]:
    found = list(pattern.finditer(source))
    if len(found) != 1:
        raise ValueError(f"share fact: {fact} must appear exactly once")
    return found[0]


def _date(match: re.Match[str]) -> str:
    return dt.date(*(int(piece) for piece in match.groups())).isoformat()


def _count(value: str) -> int:
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value):
        raise ValueError("share fact: invalid share count")
    return int(value.replace(",", ""))


def parse_listing(raw: bytes, entry: dict, priority: dict, *, ticker: str) -> ReviewedShareCountChange:
    """Read only the ordinary-common line in a pinned completed listing notice."""
    source = raw.decode("utf-8")
    codes = [(isin, short) for isin, short in _CODE.findall(source) if short == ticker]
    if len(codes) != 1 or codes[0][0] != SECURITY_IDS.get(ticker) or "보통주" not in source:
        raise ValueError("share fact: source security or common class differs")
    issuer_marker = entry.get("expected_text")
    if not isinstance(issuer_marker, str) or issuer_marker not in source:
        raise ValueError("share fact: source issuer differs")
    if "변경상장(주식소각)" in source and "변경사유 : 주식소각" in source:
        kind = "treasury_cancellation"
        amounts = _one(_CANCELLATION, source, "common cancellation count")
        before, after = (_count(piece) for piece in amounts.groups())
        effective = _date(_one(_EFFECTIVE_CANCEL, source, "cancellation date"))
        listing = _date(_one(_LISTING_CANCEL, source, "change listing date"))
        first_delta = re.search(r"변경주식수\s*:\s*-([\d,]+)주", source[amounts.end():])
        if first_delta is None or _count(first_delta.group(1)) != before - after:
            raise ValueError("share fact: cancellation delta differs")
    elif "추가상장" in source and "증자방법 : 주식매수선택권 행사" in source:
        kind = "option_exercise_issuance"
        amount = _count(_one(_ISSUANCE, source, "common issuance count").group(1))
        before, after = priority["listed_shares_before"], priority["listed_shares_after"]
        if after - before != amount:
            raise ValueError("share fact: issuance delta differs")
        effective = _date(_one(_EFFECTIVE_ISSUE, source, "issuance date"))
        listing = _date(_one(_LISTING_ISSUE, source, "additional listing date"))
    else:
        raise ValueError("share fact: unsupported or ambiguous listing type")
    if ((before, after, listing) != (
            priority["listed_shares_before"], priority["listed_shares_after"],
            priority["krx_change_session"])):
        raise ValueError("share fact: original and frozen KRX observation differ")
    return ReviewedShareCountChange(
        ticker=ticker, security_id=codes[0][0], share_class="ordinary_common",
        kind=kind, effective_on=effective, listing_session=listing,
        previous_session=priority["previous_session"],
        listed_shares_before=before, listed_shares_after=after,
        source_url=entry["url"], source_sha256=hashlib.sha256(raw).hexdigest(),
        source_filing_date=entry["source_filing_date"],
        source_observed_at=dt.datetime.fromisoformat(entry["retrieved_at_utc"]),
        source_span=("기명식 보통주, 발행일(소각일), 변경상장일, 변경사유, 코드"
                     if kind == "treasury_cancellation" else
                     "기명식 보통주, 발행일, 상장일, 증자방법, 코드"))


def review(data_dir: Path, priority_path: Path = PRIORITY,
           sources: tuple = SOURCES) -> dict:
    frozen = priority_path.read_bytes()
    if hashlib.sha256(frozen).hexdigest() != PRIORITY_SHA256:
        raise ValueError("share fact: frozen priority SHA differs")
    priority = json.loads(frozen)
    rows = priority.get("events", [])
    if (priority.get("schema") != "research-held-share-change-priority-v1"
            or priority.get("distinct_issuer_sessions") != 7
            or priority.get("overlap_episode_refs") != 13
            or priority.get("left_censored_overlap_refs") != 2
            or len(rows) != 7):
        raise ValueError("share fact: frozen priority scope differs")
    keyed = {(row["ticker"], row["krx_change_session"]): row for row in rows}
    if len(keyed) != len(rows) or len(sources) != 7:
        raise ValueError("share fact: duplicate or missing issuer session")

    archive_checks: dict[str, dict] = {}
    facts = []
    seen_sources = set()
    for filename, expected_sha, document_id, ticker in sources:
        if document_id in seen_sources:
            raise ValueError("share fact: duplicate official source")
        seen_sources.add(document_id)
        path = data_dir / filename
        if filename not in archive_checks:
            checked = verify_action_archive(path)
            if checked["sha256"] != expected_sha or checked["source"] != "KRX KIND originals":
                raise ValueError("share fact: original archive SHA/source differs")
            archive_checks[filename] = checked
        member = f"raw/{document_id}.htm"
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            entry = manifest["entries"].get(member)
            if entry is None or entry["document_id"] != document_id:
                raise ValueError("share fact: official document absent")
            candidate = [row for row in rows if row["ticker"] == ticker
                         and row["krx_change_session"] ==
                         _listing_in_source(archive.read(member))]
            if len(candidate) != 1:
                raise ValueError("share fact: official document does not bind one frozen event")
            fact = parse_listing(archive.read(member), entry, candidate[0], ticker=ticker)
        if (fact.ticker, fact.listing_session) not in keyed:
            raise ValueError("share fact: fact is not in frozen source queue")
        facts.append(fact)
    if len({(fact.ticker, fact.listing_session) for fact in facts}) != 7:
        raise ValueError("share fact: selected sources do not cover seven events")
    encoded = []
    for fact in sorted(facts, key=lambda item: (item.ticker, item.listing_session)):
        item = asdict(fact)
        item["source_observed_at"] = fact.source_observed_at.isoformat()
        item["event_ref"] = f"{fact.ticker}:{fact.listing_session}"
        item["episode_refs"] = keyed[(fact.ticker, fact.listing_session)]["episode_refs"]
        item["source_availability"] = "retrospective_observation"
        item["correction_scope"] = "selected_completed_listing_only"
        encoded.append(item)
    return {"schema": "research-reviewed-share-count-change-v1", "research_only": True,
            "source_queue_sha256": PRIORITY_SHA256,
            "archives_sha256": {name: row["sha256"] for name, row in sorted(archive_checks.items())},
            "facts": encoded, "distinct_issuer_sessions": 7,
            "overlap_episode_refs": sum(len(item["episode_refs"]) for item in encoded),
            "left_censored_overlap_refs": sum(ref["left_censored"] for item in encoded
                                              for ref in item["episode_refs"]),
            "coverage_state": "partial", "economic_return_pct": None,
            "strict_pit_eligible": False, "registered_verdict": False,
            "live_order_eligible": False}


def _listing_in_source(raw: bytes) -> str:
    text = raw.decode("utf-8")
    pattern = _LISTING_CANCEL if "변경상장(주식소각)" in text else _LISTING_ISSUE
    return _date(_one(pattern, text, "listing date"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = review(args.data_dir)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "events": len(report["facts"]),
                      "coverage_state": report["coverage_state"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
