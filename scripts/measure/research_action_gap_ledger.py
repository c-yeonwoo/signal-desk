"""Reproducible, price-panel-wide corporate-action *gap* inventory.

The seven inspected development windows and top-200 construction are fixed.
This script never calls a provider or certifies that an unobserved action did
not occur. It does not feed the registered harness or trading simulator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import (
    SourceError, compare_dart_kis_dividend, extract_dart_common_dividend,
    extract_kis_schedule_facts,
    source_window_from_archive, verify_action_archive,
)
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import (
    ARCHIVES, HOLDOUT_ARCHIVES, HOLDOUT_WINDOWS, PILOTS,
)
from signal_desk import market_clock
from signal_desk.signals.engine import SignalConfig

HORIZON_TAIL = 21  # h20: next open plus the possible exit session
PROTECTED_START = "2026-08-05"


def _top200(rows: list[dict]) -> list[tuple[str, str]]:
    """Exactly the pilot's raw cap-ranked common-share universe, pre-gates."""
    candidates = []
    for row in rows:
        ticker = str(row.get("ISU_CD") or "")
        cap = _number(row.get("MKTCAP"))
        if len(ticker) == 6 and ticker.endswith("0") and cap is not None and cap > 0:
            candidates.append((cap, ticker, str(row.get("ISU_NM") or ticker)))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    if len(candidates) < 200:
        raise ValueError("fewer than 200 valid common-share candidates")
    return [(ticker, name) for _, ticker, name in candidates[:200]]


def _frozen_windows(data_dir: Path) -> list[tuple[str, str, str, tuple[str, ...]]]:
    windows = [(name, start, end, HOLDOUT_ARCHIVES)
               for name, start, end in HOLDOUT_WINDOWS]
    for name, (filename, expected_sha) in PILOTS.items():
        raw = (data_dir / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ValueError(f"{name}: frozen pilot SHA mismatch")
        pilot = json.loads(raw)
        dates = pilot.get("signal_dates")
        if (pilot.get("source_level") != "C_offline_price_only_current_engine"
                or pilot.get("registered_verdict") is not False
                or not isinstance(dates, list) or not dates
                or dates != sorted(set(dates))):
            raise ValueError(f"{name}: unexpected pilot inputs")
        windows.append((name, dates[0], dates[-1], ARCHIVES))
    if len(windows) != 7 or any(end >= PROTECTED_START for _, _, end, _ in windows):
        raise ValueError("the frozen seven-window development scope changed")
    return windows


def _saved_action_evidence(paths: tuple[Path, ...]) -> tuple[dict[str, list[dict]], list[dict], list[dict]]:
    """Index only saved original bytes; issuer mappings are clues, not certificates."""
    indexed: dict[str, list[dict]] = defaultdict(list)
    source_rows = []
    dart_dividends = []
    kis_dividend_paths = []
    for path in paths:
        checked = verify_action_archive(path)
        window = source_window_from_archive(path)
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if checked["source"] == "OpenDART":
                filings = [row for name in sorted(manifest["entries"])
                           if name.startswith("raw/list/")
                           for row in json.loads(archive.read(name)).get("list", [])]
                tickers = {row.get("stock_code") for row in filings
                           if isinstance(row.get("stock_code"), str)}
                # An issuer search can cover multiple classes or no listed share.
                ticker = next(iter(tickers)) if len(tickers) == 1 else None
                if ticker:
                    corrections = [row["rcept_no"] for row in filings
                                   if "정정" in str(row.get("report_nm") or "")]
                    indexed[ticker].append({"kind": "filing_window", "source_sha256": checked["sha256"],
                                            "date_basis": "receipt", "start": window.start,
                                            "end": window.end, "filings": len(filings),
                                            "correction_receipts_in_window": corrections,
                                            "complete_action_scope": False})
                    for row in filings:
                        if str(row.get("report_nm") or "").strip() != "현금ㆍ현물배당결정":
                            continue
                        try:
                            fact = extract_dart_common_dividend(path, row["rcept_no"])
                            dart_dividends.append(fact)
                            indexed[ticker].append({"kind": "cash_dividend_schedule",
                                                    "source": "DART", "event_id": fact.event_id,
                                                    "receipt_date": fact.availability.published_on,
                                                    "record_date": fact.record_date,
                                                    "amount_krw": fact.cash_per_share,
                                                    "scheduled_payment_date": fact.scheduled_payment_date,
                                                    "ex_date": None, "actual_payment_date": None,
                                                    "raw_sha256": fact.raw_sha256})
                        except (SourceError, ValueError):
                            indexed[ticker].append({"kind": "unparsed_dividend_filing",
                                                    "receipt": row["rcept_no"],
                                                    "source_sha256": checked["sha256"]})
            else:
                ticker = manifest["ticker"]
                if manifest["endpoint"].endswith("/dividend"):
                    kis_dividend_paths.append((ticker, path))
                indexed[ticker].append({"kind": "schedule_window", "source_sha256": checked["sha256"],
                                        "date_basis": "unknown", "start": window.start,
                                        "end": window.end, "rows": manifest["total_rows"],
                                        "complete_action_scope": False})
                try:
                    facts = extract_kis_schedule_facts(path)
                    for fact in facts:
                        indexed[ticker].append({"kind": fact.kind + "_schedule", "source": "KIS",
                                                "event_id": fact.event_id,
                                                "record_date": fact.record_date,
                                                "amount_krw": fact.cash_per_share,
                                                "scheduled_payment_date": fact.scheduled_payment_date,
                                                "listing_session": fact.listing_session,
                                                "effective_session": None,
                                                "ex_date": None,
                                                "actual_payment_date": None,
                                                "raw_sha256": fact.raw_sha256})
                except (SourceError, ValueError) as exc:
                    indexed[ticker].append({"kind": "unparsed_schedule",
                                            "reason": str(exc), "source_sha256": checked["sha256"]})
        source_rows.append({"archive": path.name, "sha256": checked["sha256"],
                            "security_id": window.security_id, "date_basis": window.date_basis,
                            "start": window.start, "end": window.end,
                            "coverage": "partial"})
    comparisons = []
    for fact in dart_dividends:
        for ticker, path in kis_dividend_paths:
            if fact.security_id == f"ticker:{ticker}":
                comparisons.append(compare_dart_kis_dividend(fact, path))
    return indexed, source_rows, comparisons


def _reviewed_case_links(path: Path | None) -> tuple[dict[str, list[dict]], str | None]:
    if path is None:
        return {}, None
    raw = path.read_bytes()
    payload = json.loads(raw)
    if (payload.get("schema") != "research-action-reviewed-links-v1"
            or payload.get("raw_archived") is not False
            or not isinstance(payload.get("cases"), list)):
        raise ValueError("invalid reviewed-case link manifest")
    links: dict[str, list[dict]] = defaultdict(list)
    for case in payload["cases"]:
        if (not isinstance(case, dict) or not isinstance(case.get("ticker"), str)
                or len(case["ticker"]) != 6 or not case["ticker"].isdigit()
                or not isinstance(case.get("event_date"), str)
                or case["event_date"] >= PROTECTED_START
                or not market_clock.is_session("kr", case["event_date"])
                or not str(case.get("source_url") or "").startswith("https://kind.krx.co.kr/")
                or case.get("economic_settlement_confirmed") is not False
                or case.get("pit_eligible") is not False):
            raise ValueError("invalid or overclaimed reviewed-case link")
        links[case["ticker"]].append(case)
    return links, hashlib.sha256(raw).hexdigest()


def build_ledger(data_dir: Path, *, action_archives: tuple[Path, ...] = (),
                 reviewed_cases: Path | None = None) -> dict:
    """One row per ticker per inspected window; preserve non-buyer competitors."""
    evidence, action_sources, comparisons = _saved_action_evidence(action_archives)
    reviewed_links, reviewed_links_sha = _reviewed_case_links(reviewed_cases)
    windows, rows = [], []
    panels = {}
    warmup = max(SignalConfig().ma_long, SignalConfig().momentum_lookback + 1)
    for name, start, end, archive_names in _frozen_windows(data_dir):
        if archive_names not in panels:
            panels[archive_names] = load_krx_archives([data_dir / item for item in archive_names])
        days, prices = panels[archive_names]
        sessions = sorted(days)
        index = {day: n for n, day in enumerate(sessions)}
        decisions = [day for day in sessions if start <= day <= end]
        if not decisions or len(decisions) > 60 or decisions[0] != start or decisions[-1] != end:
            raise ValueError(f"{name}: incomplete decision sessions")
        by_ticker: dict[str, dict] = {}
        for day in decisions:
            for ticker, title in _top200(days[day]):
                item = by_ticker.setdefault(ticker, {"name": title, "candidate_sessions": []})
                item["candidate_sessions"].append(day)
        bars = {day: {str(item.get("ISU_CD") or ""): item for item in days[day]}
                for day in sessions}
        source_digest = hashlib.sha256("\n".join(
            f"{item['archive']}:{item['sha256']}" for item in prices).encode()).hexdigest()
        window_rows = []
        for ticker, item in sorted(by_ticker.items()):
            first, last = item["candidate_sessions"][0], item["candidate_sessions"][-1]
            first_index = index[first]
            warmup_start = sessions[max(0, first_index - warmup + 1)]
            tail = market_clock.next_sessions("kr", last, HORIZON_TAIL)
            if len(tail) != HORIZON_TAIL or tail[-1] not in index:
                raise ValueError(f"{name}: price archive does not cover the h20 tail")
            scope = sessions[index[warmup_start]:index[tail[-1]] + 1]
            missing = [day for day in scope if ticker not in bars[day]]
            observed_days = [day for day in scope if ticker in bars[day]]
            first_observed = observed_days[0] if observed_days else None
            last_observed = observed_days[-1] if observed_days else None
            missing_before_first = sum(first_observed is not None and day < first_observed
                                       for day in missing)
            missing_after_last = sum(last_observed is not None and day > last_observed
                                     for day in missing)
            missing_interior = len(missing) - missing_before_first - missing_after_last
            share_changes = []
            prior_shares = None
            for day in scope:
                raw = bars[day].get(ticker)
                value = _number(raw.get("LIST_SHRS")) if raw else None
                shares = int(value) if value is not None and value > 0 and value.is_integer() else None
                if prior_shares is not None and shares is not None and shares != prior_shares:
                    share_changes.append(day)
                prior_shares = shares
            related = [fact for fact in evidence.get(ticker, []) if (
                # Retrieval windows are relevant when they overlap the scope;
                # individual events can also have an earlier record date.
                (fact.get("start") and fact["start"] <= scope[-1] and fact["end"] >= scope[0])
                or (fact.get("record_date") and scope[0] <= fact["record_date"] <= scope[-1])
                or (fact.get("receipt_date") and scope[0] <= fact["receipt_date"] <= scope[-1])
            )]
            row = {"window": name, "ticker": ticker, "name": item["name"],
                   "candidate_sessions": len(item["candidate_sessions"]),
                   "first_candidate": first, "last_candidate": last,
                   "required_scope_start": warmup_start, "required_scope_end": scope[-1],
                   "price_warmup_complete": first_index >= warmup - 1,
                   "raw_price_sessions": len(scope) - len(missing),
                   "missing_raw_price_sessions": len(missing),
                   "missing_before_first_price": missing_before_first,
                   "missing_between_first_and_last_price": missing_interior,
                   "missing_after_last_price": missing_after_last,
                   "first_missing_raw_price": missing[0] if missing else None,
                   "listed_share_change_dates": share_changes,
                   "saved_action_evidence": related,
                   "reviewed_url_only_cases": [case for case in reviewed_links.get(ticker, [])
                                               if scope[0] <= case["event_date"] <= scope[-1]],
                   "action_coverage": "partial",
                   "gaps": ["effective_date_catalog_unverified", "all_action_kinds_unverified",
                            "correction_chain_unverified",
                            *(["price_warmup_source_incomplete"] if first_index < warmup - 1 else []),
                            *(["pre_first_price_listing_or_transfer_unverified"] if missing_before_first else []),
                            *(["interior_price_or_listing_gap_unresolved"] if missing_interior else []),
                            *(["post_last_price_cause_unverified"] if missing_after_last else []),
                            *(["share_count_change_type_unverified"] if share_changes else []),
                            *(["known_dividend_ex_date_and_payment_unverified"] if any(
                                fact["kind"] == "cash_dividend_schedule" for fact in related) else [])]}
            window_rows.append(row)
        rows.extend(window_rows)
        windows.append({"window": name, "decision_start": start, "decision_end": end,
                        "decision_sessions": len(decisions), "candidate_slots": len(decisions) * 200,
                        "unique_tickers": len(window_rows), "price_archive_digest": source_digest,
                        "tickers_with_share_changes": sum(bool(r["listed_share_change_dates"])
                                                          for r in window_rows),
                        "tickers_with_saved_action_evidence": sum(bool(r["saved_action_evidence"])
                                                                 for r in window_rows)})
    return {"schema": "research-action-gap-ledger-v1", "registered_verdict": False,
            "strict_pit_eligible": False, "verified_total_return": False,
            "protected_start": PROTECTED_START, "warmup_sessions": warmup,
            "tail_sessions": HORIZON_TAIL, "windows": windows,
            "action_sources": action_sources, "schedule_comparisons": comparisons,
            "reviewed_case_links_sha256": reviewed_links_sha,
            "ticker_rows": rows,
            "limitations": ["top-200 market-cap common shares are not KOSPI200 constituents",
                            "listed-share changes are flags, not proof of action type or ratio",
                            "no saved event does not prove absence of an event",
                            "DART receipt and KIS record/listing dates do not certify ex/effective dates",
                            "scheduled payment is not confirmed payment or tradeable cash"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--action-archive", type=Path, action="append", default=[])
    parser.add_argument("--reviewed-cases", type=Path,
                        help="optional link-only case notes; never treated as raw coverage")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; no evidence artifact is overwritten")
    report = build_ledger(args.data_dir, action_archives=tuple(args.action_archive),
                          reviewed_cases=args.reviewed_cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # One ticker per line keeps the generated artifact reviewable.
    manifest = {key: value for key, value in report.items() if key != "ticker_rows"}
    def encode(row: dict) -> str:
        return json.dumps(row, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(encode({"type": "manifest", **manifest}) + "\n")
        for row in report["ticker_rows"]:
            handle.write(encode({"type": "ticker_gap", **row}) + "\n")
    print(json.dumps({"output": str(args.output), "windows": len(report["windows"]),
                      "ticker_rows": len(report["ticker_rows"]),
                      "verified_total_return": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
