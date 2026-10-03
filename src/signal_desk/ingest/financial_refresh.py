"""Small, bounded DART evidence refresh for watched Korean companies.

Only an observed financial evidence archive is written. This code neither
updates the existing fundamental factor cache nor invokes a trading decision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Callable

from signal_desk.ingest import financial_evidence as evidence

MAX_ISSUERS_PER_DAY = 4
MAX_REQUESTS_PER_DAY = 8


def report_window(day: dt.date) -> tuple[str, str]:
    """Conservative disclosure window; absent filings remain absent."""
    if day.month >= 12:
        return str(day.year), "11014"        # third quarter, generally after November
    if day.month >= 9:
        return str(day.year), "11012"        # half year, generally after August
    if day.month >= 6:
        return str(day.year), "11013"        # first quarter, generally after May
    if day.month >= 4:
        return str(day.year - 1), "11011"    # annual, generally after March
    return str(day.year - 1), "11014"


def attempt_key(target: evidence.Target) -> str:
    return "financial_evidence_attempt:" + hashlib.sha256(target.url.encode()).hexdigest()[:24]


def budget_key(day: dt.date) -> str:
    return f"financial_evidence_requests:{day.isoformat()}"


def _due(path: Path, target: evidence.Target, now: dt.datetime,
         last_attempt: Callable[[str], object], *, prior: bool) -> bool:
    current = evidence.latest(path, target, as_of=now.isoformat())
    if current:
        days = 180 if prior else 7 if current["status"] == "no_data" else 14
        observed = dt.datetime.fromisoformat(current["available_at"])
        if now - observed < dt.timedelta(days=days):
            return False
    attempt = last_attempt(attempt_key(target)) or {}
    if isinstance(attempt, dict) and attempt.get("at") is not None:
        try:
            age = now.timestamp() - float(attempt["at"])
            status = attempt.get("status")
            # A crash or upstream failure must not trigger another request each loop.
            if age < (2 * 86400 if status in {"started", "collection_failed"} else 86400):
                return False
        except (TypeError, ValueError):
            return False
    return True


def plan(path: Path, favorites: list[str], corp_codes: dict[str, str], *, now: dt.datetime,
         last_attempt: Callable[[str], object], max_issuers: int = MAX_ISSUERS_PER_DAY,
         max_requests: int = MAX_REQUESTS_PER_DAY) -> list[evidence.Target]:
    """Read-only fair batch plan: missing/oldest attempts before recently checked."""
    if now.tzinfo is None or now.utcoffset() is None or max_issuers < 0 or max_requests < 0:
        raise ValueError("timezone and nonnegative budgets required")
    year, report = report_window(now.date())
    candidates = []
    seen_issuers = set()
    for ticker in sorted(set(favorites)):
        if not re.fullmatch(r"[0-9]{6}", ticker):
            continue
        issuer = corp_codes.get(ticker)
        if not isinstance(issuer, str) or not re.fullmatch(r"[0-9]{8}", issuer):
            continue
        if issuer in seen_issuers:
            continue
        seen_issuers.add(issuer)
        target = evidence.Target("dart", issuer, year, report)
        previous = evidence.Target("dart", issuer, str(int(year) - 1), report)
        due = [candidate for candidate, prior in ((target, False), (previous, True))
               if _due(path, candidate, now, last_attempt, prior=prior)]
        if due:
            stamps = []
            for item in due:
                attempt = last_attempt(attempt_key(item))
                try:
                    stamps.append(float(attempt.get("at", 0)) if isinstance(attempt, dict) else 0)
                except (TypeError, ValueError):
                    stamps.append(0)
            candidates.append((min(stamps), ticker, due))
    candidates.sort(key=lambda item: (item[0], item[1]))
    out = []
    for _stamp, _ticker, due in candidates[:max_issuers]:
        for target in due:
            if len(out) == max_requests:
                return out
            out.append(target)
    return out


def run(path: Path, favorites: list[str], corp_codes: dict[str, str], *, now: dt.datetime,
        dart_key: str, attempt_get: Callable[[str], object],
        attempt_set: Callable[[str, object], None], reserve: Callable[[str], bool],
        collector: Callable[..., dict] = evidence.collect) -> dict:
    """Run a bounded batch with atomic daily request reservations."""
    if not dart_key:
        return {"status": "missing_credentials", "requested": 0, "ok": 0, "no_data": 0,
                "failed": 0, "response_bytes": 0}
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone required")
    daily_budget = budget_key(now.date())
    raw_used = attempt_get(daily_budget)
    # The stored counter is an integer. A malformed value must fail closed,
    # never be coerced to a smaller allowance (bool is an int in Python).
    if raw_used is None:
        used = 0
    elif (isinstance(raw_used, int) and not isinstance(raw_used, bool)
          and 0 <= raw_used <= MAX_REQUESTS_PER_DAY):
        used = raw_used
    else:
        used = MAX_REQUESTS_PER_DAY
    remaining = MAX_REQUESTS_PER_DAY - used
    targets = plan(path, favorites, corp_codes, now=now, last_attempt=attempt_get,
                   max_requests=remaining) if remaining else []
    summary = {"status": "ok", "requested": 0, "ok": 0, "no_data": 0,
               "failed": 0, "response_bytes": 0, "raw_changed": 0, "at": now.isoformat()}
    if not remaining:
        summary["status"] = "budget_exhausted"
        return summary
    for target in targets:
        key = attempt_key(target)
        mark = {"at": int(now.timestamp()), "status": "started"}
        try:
            # Two workers may plan from the same snapshot. Reserve atomically
            # before contacting DART so their combined calls stay within 8.
            if not reserve(daily_budget):
                summary["status"] = "budget_exhausted"
                break
            attempt_set(key, mark)
        except Exception:
            # No collector call occurred. Do not count a reserved-but-unused
            # allowance as a network request, and stop on broken state storage.
            summary["status"] = "state_failure"
            break
        try:
            result = collector(path, target, dart_key=dart_key)
            if not isinstance(result, dict):
                result = {"status": "collection_failed", "response_bytes": 0}
        except Exception:
            result = {"status": "collection_failed", "response_bytes": 0}
        try:
            attempt_set(key, {**mark, "status": result.get("status", "collection_failed")})
        except Exception:
            # Preserve the collector's actual outcome/bytes even if its cooldown
            # marker cannot be finalized. The pre-request reservation remains.
            summary["status"] = "state_failure"
        summary["requested"] += 1
        if result["status"] == "ok":
            summary["ok"] += 1
        elif result["status"] == "no_data":
            summary["no_data"] += 1
        else:
            summary["failed"] += 1
        summary["response_bytes"] += int(result.get("response_bytes") or 0)
        summary["raw_changed"] += int(bool(result.get("raw_changed")))
        if summary["status"] == "state_failure":
            break
    if summary["failed"] and summary["status"] == "ok":
        summary["status"] = "partial_failure"
    return summary
