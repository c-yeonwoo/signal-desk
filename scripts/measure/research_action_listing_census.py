"""Check the bounded KIND listing classification against frozen KRX first closes.

This validates source *references* and dates, not entitlement, total return,
historical publication-time availability, or the contents of a live provider.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
READINESS = ROOT / "docs/research-action-input-readiness-2026-10-12.json"
CENSUS = ROOT / "docs/research-action-listing-census-2026-10-12.json"
KIND_PLAN = ROOT / "docs/research-action-listing-census-kind-plan-2026-10-12.json"
LISTING_PLAN = ROOT / "docs/research-action-listing-history-plan-2026-10-12.json"
ALREADY_REVIEWED = frozenset({"022100", "034230", "0126Z0"})
EVENTS = frozenset({"new_listing", "new_listing_infrastructure_company",
                    "kosdaq_to_kospi", "demerger_relisting"})


def check(readiness_path: Path = READINESS, census_path: Path = CENSUS,
          plan_path: Path = KIND_PLAN, listing_plan_path: Path = LISTING_PLAN) -> dict:
    raw = readiness_path.read_bytes()
    census = json.loads(census_path.read_text(encoding="utf-8"))
    if (census.get("schema") != "research-action-listing-census-v1"
            or census.get("research_only") is not True
            or census.get("input_sha256") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("listing census: frozen input identity differs")
    readiness = json.loads(raw)
    observed = {}
    for window in readiness["windows"]:
        for row in window["flagged_ticker_rows"]:
            if not row["missing_before_first_price"]:
                continue
            ticker = row["ticker"]
            identity = (row["name"], row["first_valid_close_krx_session"])
            if ticker in observed and observed[ticker] != identity:
                raise ValueError(f"listing census: conflicting KRX first close for {ticker}")
            observed[ticker] = identity
    expected = set(observed) - ALREADY_REVIEWED
    seen, sources, counts, external, by_source = set(), set(), Counter(), set(), {}
    for record in census["records"]:
        ticker, source = record["ticker"], record["source"]
        url = urlsplit(source)
        if (ticker in seen or ticker not in expected or source in sources
                or (record["name"], record["first_close"]) != observed[ticker]
                or record["event"] not in EVENTS
                or url.scheme != "https" or url.hostname != "kind.krx.co.kr"
                or not (url.path.startswith("/external/")
                        or url.path.startswith("/listinvstg/"))):
            raise ValueError(f"listing census: invalid source or KRX identity for {ticker}")
        seen.add(ticker)
        sources.add(source)
        by_source[source] = record
        if url.path.startswith("/external/"):
            external.add(source)
        counts[record["event"]] += 1
    if seen != expected or len(observed) != 20:
        raise ValueError(f"listing census: missing or extra first-price gaps {sorted(expected - seen)}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    archived = [item["url"] for item in plan["documents"]]
    if (plan.get("schema") != "research-kind-review-plan-v1" or len(archived) != 9
            or set(archived) != external or len(sources - external) != 8):
        raise ValueError("listing census: archive plan and source references differ")
    listing_plan = json.loads(listing_plan_path.read_text(encoding="utf-8"))
    listing_docs = listing_plan.get("documents", [])
    listing_urls = {item["url"] for item in listing_docs}
    if (listing_plan.get("schema") != "research-kind-listing-plan-v1"
            or len(listing_docs) != 6 or len(listing_urls) != 6
            or not listing_urls <= sources - external
            or any((item["expected_text"], item["expected_listing_date"])
                   != (by_source[item["url"]]["name"], by_source[item["url"]]["first_close"])
                   for item in listing_docs)
            or {by_source[url]["ticker"] for url in (sources - external) - listing_urls}
            != {"279570", "439260"}):
        raise ValueError("listing census: listing-history archive plan differs")
    return {"schema": census["schema"], "research_only": True,
            "classified_remaining": len(seen), "classified_total_with_prior": len(observed),
            "by_event": dict(sorted(counts.items())),
            "archived_originals": len(archived) + len(listing_docs), "url_only": 2,
            "source_preservation": "fifteen_archived_two_url_only",
            "action_coverage": "partial", "verified_total_return": False,
            "strict_pit_eligible": False}


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False, sort_keys=True))
