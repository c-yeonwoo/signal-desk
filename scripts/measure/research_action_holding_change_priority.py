"""Locate share-count changes during frozen price-only first-BUY h20 episodes.

This is a source-review queue, not proof of an economic action, account holding,
historical eight-factor signal, adjusted return, or trading recommendation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import ARCHIVES, PILOTS

LEDGER = ROOT / "docs/research-action-gap-ledger-2026-10-11.jsonl"
LEDGER_SHA256 = "7c3daa5edb5a1f891854cc5aa2e9f8c1635f4c21a16e2e808cbcac7fba9abfef"
PROTECTED_START = "2026-08-05"


def _shares(days: dict[str, list[dict]], day: str, ticker: str) -> int:
    matches = [row for row in days[day] if row.get("ISU_CD") == ticker]
    if len(matches) != 1:
        raise ValueError(f"holding change: missing or duplicate KRX row {ticker} {day}")
    value = _number(matches[0].get("LIST_SHRS"))
    if value is None or value <= 0 or not value.is_integer():
        raise ValueError(f"holding change: invalid KRX share count {ticker} {day}")
    return int(value)


def analyze(rows: list[dict], pilots: dict[str, dict],
            days: dict[str, list[dict]]) -> dict:
    """Use source flags to choose which original events to inspect first.

    A repeated BUY episode for the same issuer/date is one source event, not
    multiple independent corporate actions or return observations.
    """
    keyed = {(row["window"], row["ticker"]): row for row in rows}
    if len(keyed) != len(rows):
        raise ValueError("holding change: duplicate ledger ticker-window")
    sessions = sorted(days)
    position = {day: n for n, day in enumerate(sessions)}
    events: dict[tuple[str, str], dict] = {}
    total_episodes = 0
    for window, pilot in pilots.items():
        dates = pilot["signal_dates"]
        if (pilot.get("registered_verdict") is not False
                or pilot.get("source_level") != "C_offline_price_only_current_engine"
                or not dates or dates != sorted(set(dates)) or dates[-1] >= PROTECTED_START):
            raise ValueError("holding change: pilot scope or source level differs")
        episodes = pilot["buy_episodes"]["episodes"]
        if len(episodes) != pilot["buy_episodes"]["count"]:
            raise ValueError("holding change: pilot episode count differs")
        total_episodes += len(episodes)
        for episode in episodes:
            ticker = episode["ticker"]
            ledger = keyed.get((window, ticker))
            if ledger is None:
                raise ValueError(f"holding change: missing ledger row {window} {ticker}")
            h20 = episode["h20"]
            if h20.get("state") != "matured":
                continue
            entry, exit_day = h20["entry_date"], h20["exit_date"]
            overlap = [day for day in ledger["listed_share_change_dates"]
                       if entry <= day <= exit_day]
            if bool(overlap) != (episode.get("h20_share_status") == "listed_shares_changed"):
                raise ValueError(f"holding change: KRX and pilot share flags differ {window} {ticker}")
            for day in overlap:
                index = position.get(day)
                if index is None or index == 0:
                    raise ValueError(f"holding change: no previous KRX session {ticker} {day}")
                previous = sessions[index - 1]
                before, after = _shares(days, previous, ticker), _shares(days, day, ticker)
                if before == after:
                    raise ValueError(f"holding change: KRX share count did not change {ticker} {day}")
                key = ticker, day
                item = events.setdefault(key, {"ticker": ticker, "name": ledger["name"],
                                               "krx_change_session": day,
                                               "previous_session": previous,
                                               "listed_shares_before": before,
                                               "listed_shares_after": after,
                                               "listed_shares_delta": after - before,
                                               "event_type": "unverified",
                                               "effective_date": None,
                                               "correction_chain": "unverified",
                                               "cash_or_share_entitlement": "unverified",
                                               "episode_refs": []})
                if (item["name"] != ledger["name"] or item["listed_shares_before"] != before
                        or item["listed_shares_after"] != after):
                    raise ValueError(f"holding change: inconsistent repeated event {ticker} {day}")
                item["episode_refs"].append({"window": window,
                                             "first_buy_date": episode["first_buy_date"],
                                             "left_censored": bool(episode["left_censored"])})
    ordered = [events[key] for key in sorted(events)]
    for item in ordered:
        item["episode_refs"].sort(key=lambda row: (row["window"], row["first_buy_date"]))
    return {"schema": "research-held-share-change-priority-v1", "research_only": True,
            "strict_pit_eligible": False, "verified_total_return": False,
            "registered_verdict": False, "price_only_episodes": total_episodes,
            "overlap_episode_refs": sum(len(item["episode_refs"]) for item in ordered),
            "left_censored_overlap_refs": sum(ref["left_censored"] for item in ordered
                                              for ref in item["episode_refs"]),
            "distinct_issuer_sessions": len(ordered), "events": ordered,
            "limitations": ["Share-count delta is not a corporate-action classification or entitlement",
                            "Four price-only pilot windows only; earlier three windows have no BUY episodes in this queue",
                            "A simulated h20 episode is not an actual account position",
                            "Repeated episodes are not independent observations",
                            "All 1,488 ticker-window rows retain partial action coverage"]}


def run(data_dir: Path) -> dict:
    raw = LEDGER.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LEDGER_SHA256:
        raise ValueError("holding change: frozen gap ledger SHA differs")
    lines = [json.loads(line) for line in raw.splitlines()]
    manifest, rows = lines[0], lines[1:]
    if (manifest.get("schema") != "research-action-gap-ledger-v1"
            or manifest.get("registered_verdict") is not False
            or len(rows) != 1488 or any(row.get("type") != "ticker_gap" for row in rows)):
        raise ValueError("holding change: unexpected frozen ledger structure")
    pilots = {}
    for window, (filename, expected_sha) in PILOTS.items():
        pilot_raw = (data_dir / filename).read_bytes()
        if hashlib.sha256(pilot_raw).hexdigest() != expected_sha:
            raise ValueError(f"holding change: frozen pilot SHA differs {window}")
        pilots[window] = json.loads(pilot_raw)
    days, sources = load_krx_archives([data_dir / name for name in ARCHIVES])
    result = analyze(rows, pilots, days)
    result["gap_ledger_sha256"] = LEDGER_SHA256
    result["frozen_pilot_sha256"] = {window: sha for window, (_, sha) in PILOTS.items()}
    result["krx_sources"] = sources
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.data_dir)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output),
                      "distinct_issuer_sessions": report["distinct_issuer_sessions"],
                      "overlap_episode_refs": report["overlap_episode_refs"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
