"""Audit ticker-level price readiness in the seven frozen development windows.

This is a research diagnostic, not an action-coverage certificate or a replay
verdict. It checks the saved gap ledger against the original KRX responses and
reproduces the pilot's 253-consecutive-close admission rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import ARCHIVES, HOLDOUT_ARCHIVES, PILOTS
from scripts.measure.research_action_gap_ledger import _top200

LEDGER = ROOT / "docs/research-action-gap-ledger-2026-10-11.jsonl"
FROZEN_LEDGER_SHA256 = "7c3daa5edb5a1f891854cc5aa2e9f8c1635f4c21a16e2e808cbcac7fba9abfef"


def load_ledger(path: Path) -> tuple[dict, list[dict], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FROZEN_LEDGER_SHA256:
        raise ValueError("frozen gap ledger SHA differs")
    lines = [json.loads(line) for line in raw.splitlines()]
    if (not lines or lines[0].get("schema") != "research-action-gap-ledger-v1"
            or lines[0].get("type") != "manifest"
            or lines[0].get("registered_verdict") is not False
            or any(row.get("type") != "ticker_gap" for row in lines[1:])):
        raise ValueError("unexpected frozen gap ledger")
    return lines[0], lines[1:], digest


def audit_window(window: dict, rows: list[dict], days: dict[str, list[dict]],
                 *, warmup: int) -> dict:
    """Reconcile every candidate day and reproduce the pilot's close rule."""
    sessions = sorted(days)
    start, end = window["decision_start"], window["decision_end"]
    decisions = [day for day in sessions if start <= day <= end]
    if (len(decisions) != window["decision_sessions"] or not decisions
            or decisions[0] != start or decisions[-1] != end):
        raise ValueError(f"{window['window']}: decision sessions differ")
    by_ticker = {row["ticker"]: row for row in rows}
    if len(by_ticker) != len(rows) or len(rows) != window["unique_tickers"]:
        raise ValueError(f"{window['window']}: ticker ledger differs")
    candidate_dates: dict[str, list[str]] = defaultdict(list)
    for day in decisions:
        for ticker, _ in _top200(days[day]):
            candidate_dates[ticker].append(day)
    if (set(candidate_dates) != set(by_ticker)
            or sum(map(len, candidate_dates.values())) != window["candidate_slots"]):
        raise ValueError(f"{window['window']}: candidate panel differs")
    for ticker, dates in candidate_dates.items():
        saved = by_ticker[ticker]
        if (len(dates) != saved["candidate_sessions"]
                or dates[0] != saved["first_candidate"]
                or dates[-1] != saved["last_candidate"]):
            raise ValueError(f"{window['window']}: {ticker} candidate dates differ")

    # Missing or nonpositive closes break the streak, as in run_pilot.
    streak = {ticker: 0 for ticker in candidate_dates}
    first_streak = {}
    first_valid_close = {}
    excluded = defaultdict(int)
    eligible = defaultdict(int)
    for day in sessions:
        valid = {str(row.get("ISU_CD") or "") for row in days[day]
                 if (_number(row.get("TDD_CLSPRC")) or 0) > 0}
        for ticker in streak:
            streak[ticker] = streak[ticker] + 1 if ticker in valid else 0
            if ticker in valid and ticker not in first_valid_close:
                first_valid_close[ticker] = day
        if day < start or day > end:
            continue
        for ticker, _ in _top200(days[day]):
            if ticker not in first_streak:
                first_streak[ticker] = min(streak[ticker], warmup)
            if streak[ticker] >= warmup:
                eligible[ticker] += 1
            else:
                excluded[ticker] += 1

    flagged = []
    for ticker, row in sorted(by_ticker.items()):
        if not (excluded[ticker] or row["missing_raw_price_sessions"]):
            continue
        flagged.append({"ticker": ticker, "name": row["name"],
                        "candidate_sessions": row["candidate_sessions"],
                        "first_valid_close_krx_session": first_valid_close.get(ticker),
                        "first_candidate_close_streak": first_streak[ticker],
                        "excluded_candidate_sessions": excluded[ticker],
                        "admitted_candidate_sessions": eligible[ticker],
                        "missing_before_first_price": row["missing_before_first_price"],
                        "missing_between_first_and_last_price": row["missing_between_first_and_last_price"],
                        "missing_after_last_price": row["missing_after_last_price"],
                        "action_coverage": "partial"})
    total_excluded = sum(excluded.values())
    total_admitted = sum(eligible.values())
    if total_excluded + total_admitted != window["candidate_slots"]:
        raise ValueError(f"{window['window']}: candidate accounting differs")
    return {"window": window["window"], "candidate_slots": window["candidate_slots"],
            "excluded_candidate_sessions": total_excluded,
            "admitted_candidate_sessions": total_admitted,
            "tickers_with_excluded_sessions": sum(bool(n) for n in excluded.values()),
            "flagged_ticker_rows": flagged}


def audit(data_dir: Path, ledger_path: Path = LEDGER) -> dict:
    manifest, rows, ledger_sha = load_ledger(ledger_path)
    windows = manifest["windows"]
    if len(windows) != 7 or len(rows) != 1488 or manifest["warmup_sessions"] != 253:
        raise ValueError("frozen seven-window scope differs")
    all_rows = []
    panels = {}
    for window in windows:
        archive_names = HOLDOUT_ARCHIVES if window["window"].startswith("2024-") else ARCHIVES
        if archive_names not in panels:
            panels[archive_names] = load_krx_archives([data_dir / name for name in archive_names])
        days, sources = panels[archive_names]
        digest = hashlib.sha256("\n".join(
            f"{source['archive']}:{source['sha256']}" for source in sources
        ).encode()).hexdigest()
        if digest != window["price_archive_digest"]:
            raise ValueError(f"{window['window']}: frozen KRX sources differ")
        result = audit_window(window, [row for row in rows if row["window"] == window["window"]],
                              days, warmup=manifest["warmup_sessions"])
        if window["window"] in PILOTS:
            name, expected_sha = PILOTS[window["window"]]
            raw = (data_dir / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise ValueError(f"{name}: frozen pilot SHA differs")
            pilot_excluded = json.loads(raw)["diagnostic"]["excluded_for_noncontinuous_warmup"]
            if result["excluded_candidate_sessions"] != pilot_excluded:
                raise ValueError(f"{window['window']}: pilot warmup exclusions differ")
            result["pilot_excluded_candidate_sessions"] = pilot_excluded
        all_rows.append(result)
    return {"schema": "research-action-input-readiness-v1", "research_only": True,
            "registered_verdict": False, "verified_total_return": False,
            "strict_pit_eligible": False, "action_coverage": "partial",
            "gap_ledger_sha256": ledger_sha, "windows": all_rows,
            "limitations": ["Archive-wide price_warmup_complete is not per-ticker readiness",
                            "A ticker may have valid prices before its KOSPI archive debut",
                            "Listed-share changes do not identify action types or settlement",
                            "No event absence or complete economic return is certified"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; no evidence artifact is overwritten")
    report = audit(args.data_dir, args.ledger)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output),
                      "windows": len(report["windows"]),
                      "excluded_candidate_sessions": sum(
                          row["excluded_candidate_sessions"] for row in report["windows"]),
                      "verified_total_return": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
