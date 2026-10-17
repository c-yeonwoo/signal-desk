"""Offline source bundle and derived public summary; never copy API credentials."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from scripts.measure import earnings_announcement_execution as replay
from scripts.measure.earnings_fast_entry_sources import _save
from signal_desk.ingest import alpaca_research


def paths(base: Path, kind: str) -> list[Path]:
    pattern = (r"alpaca-news-2026-10-17-b[0-9]+.json" if kind == "news" else
        r"alpaca-(reference|entry|exit)-2026-10-17-[0-9]{3}.json|alpaca-gaps-2026-10-17-r[0-9]+-b[0-9]+.json")
    return sorted(p for p in base.glob("alpaca-*.json") if re.fullmatch(pattern, p.name))


def summary(result: dict) -> dict:
    # Quote/price originals stay local under the provider's data access contract.
    public = {k: v for k, v in result.items() if k != "results"}
    public["results"] = [{k: v for k, v in row.items() if k != "rows"} for row in result["results"]]
    if "round_lot_interpretation_sensitivity" in result:
        public["round_lot_interpretation_sensitivity"] = summary(result["round_lot_interpretation_sensitivity"])
    return public


def deliver(base: Path, output: Path, archive: Path, public: Path) -> dict:
    quote_paths, news_paths = paths(base, "quotes"), paths(base, "news")
    quotes = replay.observations(quote_paths, "quote-window")
    news = replay.observations(news_paths, "news-day")
    frozen = json.loads((base / "earnings-announcement-cohort-2026-10-17-v2.json").read_bytes())
    if frozen != replay.cohort(base / "us-eps-sources-2026-10-17-v1.zip"):
        raise ValueError("frozen cohort/plan differs from the original EPS date ledger")
    inputs = replay.input_events(frozen, news)
    index = replay.quote_index(quotes)
    result = replay.report(inputs, index)
    alternate = replay.report(inputs, index, size_scale=100)
    alternate["not_a_new_independent_confirmation"] = True
    alternate["purpose"] = "Source-unit interpretation sensitivity; never replaces frozen conservative result"
    alternate["source_urls"] = [
        "https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change",
        "https://github.com/alpacahq/alpaca-skills/blob/main/skills/broker-api/market-data/SKILL.md",
        "https://forum.alpaca.markets/t/why-can-i-see-odd-lot-quotes-in-sip-feed/16000/4"]
    result["round_lot_interpretation_sensitivity"] = alternate
    result["clock_crosscheck"] = []
    primary = base / "meta-release-clocks-2026-10-17-v1.json"
    for row in json.loads(primary.read_bytes())["rows"]:
        match = re.search(r'content="([^"]+)"', " ".join(row["metadata"]))
        if not match or row["status"] != 200:
            raise ValueError("issuer wire clock unavailable")
        stamp = match[1]
        event = next(e for e in inputs["events"] if e["symbol"] == "META" and e["reported_date"] == stamp[:10])
        result["clock_crosscheck"].append({"id": event["id"], "issuer_wire_at": stamp,
            "issuer_wire_precision": "minute", "issuer_wire_url": row["url"],
            "news_created_at": event["created_at"], "news_updated_at": event["updated_at"],
            "news_max_clock_minus_wire_seconds":
                (event["ready_ns"] - 30 * replay.SECOND - replay.clock_ns(stamp)) / replay.SECOND,
            "global_first_publication_certified": False})
    result["preserved_source_counts"] = {"quote_gateway_envelopes": len(quotes),
        "unique_quote_windows": len(replay.quote_index(quotes)),
        "quote_response_rows_including_overlap": sum(len(s["body"].get("quotes") or []) for s in quotes if s["body"]),
        "quote_observation_statuses": dict(Counter(s["status"] for s in quotes)),
        "news_gateway_envelopes": len(news),
        "news_response_rows": sum(len(s["body"]["news"]) for s in news if s["body"]),
        "unique_news_ids": len({a["id"] for s in news if s["body"] for a in s["body"]["news"]}),
        "not_a_provider_bill_or_total_request_count": True}
    result["code_sha256"] = {"execution": replay.digest(Path(replay.__file__)),
                              "source_gateway": replay.digest(Path(alpaca_research.__file__)),
                              "delivery": replay.digest(Path(__file__))}
    selected = quote_paths + news_paths + [primary] + sorted(base.glob("earnings-announcement-cohort-2026-10-17-v*.json"))
    selected += sorted(base.glob("earnings-announcement-inputs-2026-10-17-v*.json"))
    files = {"sources/" + p.name: p.read_bytes() for p in selected}
    files["plan.md"] = replay.PLAN.read_bytes()
    if len(files) != len(selected) + 1:
        raise ValueError("duplicate source names")
    if archive.exists():
        manifest, preserved = replay.load_archive(archive, "earnings-announcement-source-bundle-v1")
        if preserved != files or manifest["preserved_source_counts"] != result["preserved_source_counts"]:
            raise ValueError("existing immutable bundle differs from current source observations")
    else:
        _save(archive, files, {"schema": "earnings-announcement-source-bundle-v1",
                              "preserved_source_counts": result["preserved_source_counts"],
                              "live_order_eligible": False})
    result["source_archive_sha256"] = replay.digest(archive)
    result["source_archive"] = str(archive)
    replay.save(output, result)
    replay.save(public, summary(result))
    return {"report_sha256": replay.digest(output), "archive_sha256": replay.digest(archive),
            "public_sha256": replay.digest(public), "counts": result["preserved_source_counts"],
            "verdicts": result["verdicts"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--base", type=Path, default=Path("data/research"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(deliver(args.base, args.output, args.archive, args.public), ensure_ascii=False))
