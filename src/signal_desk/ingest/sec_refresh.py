"""Low-priority, bounded SEC observations for US favorites only.

The map and companyfacts use the shared EDGAR contact/rate guard. Neither the
observations nor this schedule are signal, PIT, or order inputs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path
from typing import Callable

from signal_desk.ingest import edgar, financial_evidence, sec_issuer_map

MAX_REQUESTS_PER_MONTH = 24
MAP_RETRY_DAYS = 7
FACT_RETRY_DAYS = 14
FAILED_RETRY_DAYS = 2


def _month_key(now: dt.datetime) -> str:
    return f"sec_evidence_requests:{now:%Y-%m}"


def _attempt_key(cik: str) -> str:
    return "sec_evidence_attempt:" + hashlib.sha256(cik.encode()).hexdigest()[:24]


def _age_days(now: dt.datetime, value: object) -> float | None:
    if not isinstance(value, dict) or not isinstance(value.get("at"), str):
        return None
    try:
        at = dt.datetime.fromisoformat(value["at"])
        if at.utcoffset() is None:
            return None
        return (now - at).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def _collect_facts(path: Path, cik: str) -> dict:
    """Share the EDGAR limiter with the existing collectors; fail closed."""
    target = financial_evidence.Target("sec", cik)
    raw = edgar._get(target.url)
    if raw is None:
        return {"status": "collection_failed", "response_bytes": 0}
    if len(raw) > financial_evidence.MAX_BYTES:
        return {"status": "collection_failed", "response_bytes": len(raw), "reason": "response_too_large"}
    try:
        result = financial_evidence.archive(path, target, raw,
                                            observed_at=financial_evidence._now())
        return {**result, "response_bytes": len(raw)}
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
        return {"status": "collection_failed", "response_bytes": len(raw)}


def run(map_path: Path, facts_path: Path, favorites: list[str], *, now: dt.datetime,
        state_get: Callable[[str], object], state_set: Callable[[str, object], None],
        reserve: Callable[[str], bool], map_collect: Callable[[Path], dict] = sec_issuer_map.collect,
        facts_collect: Callable[[Path, str], dict] = _collect_facts) -> dict:
    """At most one map and one companyfacts request per run, 24/month durable cap.

    `reserve` must atomically check/increment a persistent counter. The caller
    schedules this after all market-sensitive tasks and retains the result.
    """
    if now.utcoffset() is None:
        raise ValueError("timezone required")
    now = now.astimezone(dt.timezone.utc)
    summary = {"status": "not_due", "requested": 0, "ok": 0, "failed": 0,
               "response_bytes": 0, "raw_changed": 0, "at": now.isoformat(), "map_status": None,
               "facts_status": None}
    if not edgar.available():
        return {**summary, "status": "missing_real_contact"}
    # The favorites table has no market column: numeric KR tickers never enter
    # the SEC path. Every remaining identity still requires an observed SEC map.
    candidates = sorted({t for t in favorites if isinstance(t, str) and t.isascii()
                         and 1 <= len(t) <= 10 and t[0].isalpha()
                         and all(ch.isalnum() or ch in '.-' for ch in t)})
    if not candidates:
        return {**summary, "status": "no_us_favorites"}

    try:
        observed_map = sec_issuer_map.latest(map_path, as_of=now.isoformat())
    except (OSError, ValueError, sqlite3.Error, TypeError):
        observed_map = None
        summary["map_status"] = "archive_error"
    map_attempt = state_get("sec_evidence_map_attempt")
    map_age = _age_days(now, map_attempt)
    map_stale = (observed_map is None or
                 (now - dt.datetime.fromisoformat(observed_map["available_at"])).days >= MAP_RETRY_DAYS)
    if map_stale and (map_age is None or map_age >= MAP_RETRY_DAYS):
        if not reserve(_month_key(now)):
            return {**summary, "status": "budget_exhausted"}
        state_set("sec_evidence_map_attempt", {"at": now.isoformat(), "status": "started"})
        try:
            result = map_collect(map_path)
        except Exception:
            result = {"status": "collection_failed", "response_bytes": 0}
        state_set("sec_evidence_map_attempt", {"at": now.isoformat(), "status": result.get("status")})
        summary["requested"] += 1
        summary["response_bytes"] += max(0, int(result.get("response_bytes") or 0))
        summary["map_status"] = result.get("status")
        summary["ok" if result.get("status") == "ok" else "failed"] += 1
        if result.get("status") != "ok":
            summary["status"] = "partial_failure"
            return summary
    # `lookup` also rejects an observation older than 30 days, independent of
    # the refresh cadence.
    mapped = []
    for ticker in candidates:
        item = sec_issuer_map.lookup(map_path, ticker=ticker, as_of=now.isoformat())
        if item["status"] == "mapped":
            mapped.append((ticker, item["cik"]))
    if not mapped:
        summary["status"] = "partial_failure" if summary["failed"] else "no_mapped_favorites"
        return summary

    due = []
    for ticker, cik in mapped:
        mark = state_get(_attempt_key(cik))
        age = _age_days(now, mark)
        if age is not None and age < (FAILED_RETRY_DAYS if mark.get("status") in
                                     {"started", "collection_failed"} else FACT_RETRY_DAYS):
            continue
        try:
            previous = financial_evidence.latest(facts_path, financial_evidence.Target("sec", cik),
                                                 as_of=now.isoformat())
        except (OSError, ValueError, sqlite3.Error, TypeError, KeyError):
            previous = None
        if previous and (now - dt.datetime.fromisoformat(previous["available_at"])).days < FACT_RETRY_DAYS:
            continue
        due.append((age if age is not None else 100000, ticker, cik))
    if due:
        due.sort(key=lambda row: (-row[0], row[1]))
        cik = due[0][2]
        if not reserve(_month_key(now)):
            summary["status"] = "budget_exhausted" if not summary["requested"] else "partial_failure"
            return summary
        state_set(_attempt_key(cik), {"at": now.isoformat(), "status": "started"})
        try:
            result = facts_collect(facts_path, cik)
        except Exception:
            result = {"status": "collection_failed", "response_bytes": 0}
        state_set(_attempt_key(cik), {"at": now.isoformat(), "status": result.get("status")})
        summary["requested"] += 1
        summary["response_bytes"] += max(0, int(result.get("response_bytes") or 0))
        summary["facts_status"] = result.get("status")
        summary["ok" if result.get("status") in {"ok", "no_supported_facts"} else "failed"] += 1
        summary["raw_changed"] += int(bool(result.get("raw_changed")))
    summary["status"] = "partial_failure" if summary["failed"] else "ok" if summary["requested"] else "not_due"
    return summary
