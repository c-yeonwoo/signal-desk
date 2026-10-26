"""One offline event-information check on the already-used 2025 intraday panel.

Reuses the frozen EPS/revenue/downward-guidance parser and execution ledger.
This is a diagnostic, not a new independent holdout or live trading policy.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

from scripts.measure import earnings_guidance_validation as events
from scripts.measure import intraday_confirmation as follow
from scripts.measure import intraday_orb_replay as orb
from scripts.measure.contract_event_sources import load_archive
from scripts.measure.intraday_orb_sources import _save_new

EPS_SHA = "1593c6a1934c68a307a510126b988c1be91f39435d0d810422717ec1be884701"
REFERENCE_SHA = "a4a01fc06d89fde05c3f8dcfa4e5eb7e5ec30c3d642e0a0595a0a27476849aa2"


def cohort(eps_source: Path) -> dict:
    if hashlib.sha256(eps_source.read_bytes()).hexdigest() != EPS_SHA:
        raise ValueError("fixed EPS date source differs")
    _, files = load_archive(eps_source, "us-eps-sources-v1")
    rows = []
    for symbol in orb.SYMBOLS:
        body = json.loads(files[f"raw/earnings-{symbol}.json"])
        if body.get("symbol") != symbol:
            raise ValueError("issuer differs")
        for r in body["quarterlyEarnings"]:
            day = r.get("reportedDate", "")
            if "2025-01-01" <= day <= "2025-06-26":
                rows.append({"id": symbol + ":" + day, "symbol": symbol,
                             "reported_date": day, "fiscal_end": r["fiscalDateEnding"]})
    if Counter(r["symbol"] for r in rows) != Counter({s: 2 for s in orb.SYMBOLS}):
        raise ValueError("fixed 16-event date cohort incomplete")
    return {"schema": "intraday-event-information-cohort-v1", "source_sha256": EPS_SHA,
            "events": sorted(rows, key=lambda r: r["id"]), "live_order_eligible": False,
            "policy": "existing_10min_eps5_revenue_beat_known_guidance_not_down",
            "independent_holdout": False}


def news_requests(frozen: dict) -> list[dict]:
    requests = []
    for e in frozen["events"]:
        day = dt.date.fromisoformat(e["reported_date"])
        first = dt.datetime.combine(day, dt.time(), events.old.NY)
        for symbol in ([e["symbol"], "GOOG"] if e["symbol"] == "GOOGL" else [e["symbol"]]):
            requests.append({"symbol": symbol, "day": day.isoformat(), "params": {
                "symbols": symbol, "start": first.isoformat(),
                "end": (first + dt.timedelta(days=1) - dt.timedelta(seconds=1)).isoformat(),
                "sort": "asc", "limit": "50", "include_content": "false"}})
    return requests


def news_sources(directory: Path, frozen: dict) -> list[dict]:
    allowed = {(r["symbol"], r["day"]): r["params"] for r in news_requests(frozen)}
    result, seen = [], set()
    for path in sorted(directory.rglob("*.json")):
        env = json.loads(path.read_bytes())
        request = env["request"]
        identity = env["symbol"], request["start"][:10]
        expected = allowed.get(identity)
        actual = {k: v for k, v in request.items() if k != "page_token"}
        if (expected != actual or env.get("schema") != "alpaca-historical-news-day-v1"
                or env.get("status") not in ("observed", "empty")):
            raise ValueError("news source outside frozen request or unavailable")
        raw = base64.b64decode(env["raw_b64"], validate=True)
        if len(raw) != env["raw_bytes"] or hashlib.sha256(raw).hexdigest() != env["raw_sha256"]:
            raise ValueError("news source digest differs")
        body = json.loads(raw)
        key = identity, request.get("page_token", "")
        if key in seen or not isinstance(body.get("news"), list):
            raise ValueError("duplicate news page or incompatible body")
        if any(env["symbol"] not in a["symbols"] for a in body["news"]):
            raise ValueError("news issuer differs")
        seen.add(key)
        result.append({**env, "body": body})
    # Each issuer/day must have a complete chain from its initial page. No
    # company or alias can disappear just because a positive article was found.
    for identity in allowed:
        pages = {s["request"].get("page_token", ""): s["body"] for s in result
                 if (s["symbol"], s["request"]["start"][:10]) == identity}
        token, visited = "", set()
        while token is not None:
            if token in visited or token not in pages:
                raise ValueError("news pagination incomplete")
            visited.add(token)
            token = pages[token].get("next_page_token") or None
        if visited != set(pages):
            raise ValueError("unreachable news page")
    return result


def event_session(event: dict, sessions: list[str]) -> str | None:
    """Use the news revision/processing clock, never a vendor pre/post label."""
    if event["status"] != "ready":
        return None
    for day in sessions:
        if day < event["reported_date"]:
            continue
        opening, _ = orb.session_clocks(day)
        if event["ready_ns"] <= opening + 5 * orb.MINUTE:
            return day
    return None


def normalized_sources(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Normalize issuer/year/accounting labels, never numbers or source clocks.

    Retains each original SHA; derived headline changes are separately recorded.
    The frozen 2020/2021 parser and its archived results remain reproducible.
    """
    output, changes = [], []
    for source in sources:
        articles = []
        for article in source["body"]["news"]:
            original = article["headline"]
            title = re.sub(r"^Meta Platforms(?=\s)", "Meta", original, flags=re.I)
            issuer = events.old.ISSUER_TITLES[{"GOOG": "GOOGL"}.get(source["symbol"], source["symbol"])]
            prefix = r"^(" + issuer + r"\s+Q[1-4])"
            title = re.sub(prefix + r"\s+20\d{2}\s+", r"\1 ", title, flags=re.I)
            title = re.sub(prefix + r"\s+GAAP\s+(?=EPS\b)", r"\1 ", title, flags=re.I)
            if title != original:
                changes.append({"news_id": article["id"], "source_sha256": source["raw_sha256"],
                                "original_headline_sha256": hashlib.sha256(original.encode()).hexdigest(),
                                "derived_headline": title,
                                "gaap_label": bool(re.search(r"\bGAAP\s+EPS\b", original, re.I))})
            articles.append({**article, "headline": title})
        output.append({**source, "body": {**source["body"], "news": articles}})
    return output, changes


def checked_intraday(directory: Path, reference: dict) -> str:
    files = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
             for kind in ("bars", "quotes") for p in sorted((directory / kind).rglob("*.json"))}
    if not files or files != reference["provenance"]["source_sha256"]:
        raise ValueError("intraday sources differ from preserved comparison")
    return hashlib.sha256(orb.json_bytes(files)).hexdigest()


def assess(frozen: dict, news: list[dict], bars: dict, quotes: dict, reference: dict) -> dict:
    normalized, changes = normalized_sources(news)
    inputs = events.input_events(frozen, normalized)
    gaap = {c["news_id"] for c in changes if c["gaap_label"]}
    for event in inputs["events"]:
        if event.get("eps") and event["eps"]["news_id"] in gaap:
            event["eps"]["basis"] = "gaap"
    fr = follow.plan()
    if any(e["status"] != "ready" for e in inputs["events"]):
        return {"schema": "intraday-event-information-result-v1", "events": inputs["events"],
                "headline_normalizations": changes,
                "verdict": "blocked_event_inputs", "live_order_eligible": False}
    eligible = {(e["symbol"], event_session(e, fr["evaluation_sessions"]))
                for e in inputs["events"] if e["eligible"]}
    def entry(c, rows, delay=0):
        return follow.confirmed_entry(c, rows, delay) if (c["symbol"], c["day"]) in eligible else None
    path = orb.replay(bars, quotes, fr, "rvol", bps=12, size_scale=100,
                      entry_rule=entry, cost_model=follow.cost_model)
    base = reference["paths"]["confirmed"]
    turnover = reference["paths"]["turnover_baseline"]
    splits = {}
    if path["complete"]:
        splits = {name: {"event_filter": orb.split_metrics(path, days),
                         "price_only": orb.split_metrics(base, days),
                         "turnover": orb.split_metrics(turnover, days)}
                  for name, days in fr["splits"].items()}
    return {"schema": "intraday-event-information-result-v1", "events": [
            {**e, "opening_session": event_session(e, fr["evaluation_sessions"])} for e in inputs["events"]],
            "path": path, "splits": splits, "reference_result_sha256": REFERENCE_SHA,
            "headline_normalizations": changes,
            "verdict": ("blocked_execution_inputs" if not path["complete"] else
                        "no_executable_increment" if not path["trades"] else
                        "diagnostic_only_not_adopted"),
            "independent_holdout": False, "live_order_eligible": False,
            "limits": ["previously_used_2025_prices", "fixed_survivor_panel",
                       "historical_news_arrival_and_first_version_unverified",
                       "report_day_feed_only_not_all_overnight_or_call_information",
                       "cost_and_historical_quote_size_assumptions_unchanged"]}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("freeze", "assess"))
    parser.add_argument("--eps-source", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--news", type=Path)
    parser.add_argument("--intraday", type=Path)
    parser.add_argument("--no-price-evidence", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    frozen = cohort(args.eps_source)
    if args.command == "freeze":
        print(json.dumps({"plan_sha256": _save_new(args.plan, frozen)}))
        return
    if json.loads(args.plan.read_bytes()) != frozen:
        raise ValueError("frozen event cohort differs")
    raw = args.reference.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REFERENCE_SHA:
        raise ValueError("comparison reference differs")
    reference = json.loads(raw)
    intraday_digest = checked_intraday(args.intraday, reference)
    news = news_sources(args.news, frozen)
    bars = orb.load_bars(args.intraday, known_absent=follow.verified_absence(args.no_price_evidence))
    if set(bars) != {(s, d) for s in orb.SYMBOLS for d in follow.plan()["sessions"]}:
        raise ValueError("intraday panel differs")
    result = assess(frozen, news, bars, orb.load_quotes(args.intraday), reference)
    result["provenance"] = {"plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
        "intraday_source_map_sha256": intraday_digest,
        "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (Path(__file__), Path(events.__file__), Path(follow.__file__), Path(orb.__file__))},
        "news_envelope_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(args.news.rglob("*.json"))}}
    print(json.dumps({"output_sha256": _save_new(args.output, result), "verdict": result["verdict"]}))


if __name__ == "__main__":
    main()
