"""Observed US semiconductor/electronics production, research display only.

The Federal Reserve G.17 current text file is mutable. Every retrieval is
archived with today's availability; historical values in that file are NOT
point-in-time observations for backtests or trading decisions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import urllib.request
import zlib
from pathlib import Path
from typing import Callable

SOURCE_URL = "https://www.federalreserve.gov/releases/g17/Current/ipdisk/ip_sa.txt"
SOURCE_PAGE = "https://www.federalreserve.gov/releases/g17/download.htm"
SERIES = "G3344"
HEADING = '"G3344: Semiconductor and other electronic component  NAICS=3344"'
SCHEMA = "fed-g17-g3344-v1"
DEFAULT_ARCHIVE = Path("data/raw/fed-g17.db")
MAX_BYTES = 5 * 1024 * 1024
MAX_REQUESTS_PER_MONTH = 4


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(dt.timezone.utc)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse(raw: bytes, *, as_of: dt.date) -> list[dict]:
    """Match exact Fed code/heading, monthly rows and positive finite indexes."""
    if len(raw) > MAX_BYTES:
        raise ValueError("G.17 response too large")
    lines = raw.decode("ascii").splitlines()
    if sum(line.strip() == HEADING for line in lines) != 1:
        raise ValueError("G.17 G3344 heading absent or ambiguous")
    months: dict[str, str] = {}
    started = False
    for line in lines:
        if line.strip() == HEADING:
            started = True
            continue
        if not started:
            continue
        if line.startswith('"') and not line.startswith(f'"{SERIES}"'):
            break
        if not line.startswith(f'"{SERIES}"'):
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 14 or parts[0] != f'"{SERIES}"':
            raise ValueError("G.17 row shape invalid")
        year = int(parts[1])
        if year < 1972 or year > as_of.year:
            raise ValueError("G.17 year outside observed window")
        for month, value in enumerate(parts[2:], start=1):
            if (not re.fullmatch(r"[0-9]+\.[0-9]{4}", value)
                    or not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError("G.17 index invalid")
            period = f"{year:04d}-{month:02d}"
            if period in months or period > as_of.strftime("%Y-%m"):
                raise ValueError("G.17 duplicate or future month")
            months[period] = value
    if len(months) < 13:
        raise ValueError("G.17 comparison history missing")
    return [{"period": period, "value": months[period]} for period in sorted(months)[-25:]]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS fed_g17_observations "
                 "(id TEXT PRIMARY KEY, available_at TEXT NOT NULL, envelope BLOB NOT NULL, raw_zlib BLOB NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS fed_g17_available ON fed_g17_observations(available_at)")
    return conn


def archive(path: Path, raw: bytes, *, observed_at: str, source_last_modified: str | None = None) -> dict:
    observed = _utc(observed_at)
    months = parse(raw, as_of=observed.date())
    available = max(observed, _utc(_now())).isoformat()
    envelope = {"schema": SCHEMA, "source_url": SOURCE_URL, "series": SERIES,
                "observed_at": observed.isoformat(), "available_at": available,
                "source_last_modified": source_last_modified,
                "source_published_at": None, "strict_pit_eligible": False,
                "live_eligible": False, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "months": months}
    material = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    identifier = hashlib.sha256(material).hexdigest()
    conn = _connect(path)
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO fed_g17_observations VALUES (?,?,?,?)",
                         (identifier, available, material, zlib.compress(raw, level=6)))
    finally:
        conn.close()
    return {"status": "ok", "id": identifier, "latest_period": months[-1]["period"],
            "raw_sha256": envelope["raw_sha256"], "observed_at": envelope["observed_at"],
            "available_at": available, "response_bytes": len(raw)}


def latest(path: Path, *, as_of: str) -> dict | None:
    cutoff = _utc(as_of).isoformat()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id, envelope, raw_zlib, available_at FROM fed_g17_observations "
                           "WHERE available_at<=? ORDER BY available_at DESC, rowid DESC LIMIT 1",
                           (cutoff,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    identifier, material, compressed, available = row
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not inflater.eof or inflater.unused_data:
            raise ValueError("G.17 compressed archive invalid")
        envelope = json.loads(material)
    except (zlib.error, ValueError, TypeError) as exc:
        raise ValueError("G.17 archive integrity failure") from exc
    if (len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != envelope.get("raw_sha256")
            or hashlib.sha256(material).hexdigest() != identifier
            or envelope.get("schema") != SCHEMA or envelope.get("source_url") != SOURCE_URL
            or envelope.get("available_at") != available):
        raise ValueError("G.17 archive integrity failure")
    return {"id": identifier, **envelope}


def describe(path: Path, *, as_of: str) -> dict:
    """Explain one observed vintage without assigning industry or stock direction."""
    base = {"source": "Federal Reserve G.17", "source_url": SOURCE_PAGE,
            "series_url": SOURCE_URL, "series": SERIES, "scope": "미국 내 반도체·기타 전자부품 제조",
            "unit": "2017=100 계절조정 생산지수", "mode": "research_only",
            "strict_pit_eligible": False, "live_eligible": False,
            "note": "미국 생산 통계입니다. 세계 반도체 매출·한국 메모리 업황·개별 종목 수익률과 같지 않습니다."}
    try:
        item = latest(path, as_of=as_of)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {**base, "status": "archive_error", "reason": "보존 기록을 검증할 수 없습니다."}
    if not item:
        return {**base, "status": "not_recorded", "reason": "아직 공식 생산지수 관측이 없습니다."}
    values = {row["period"]: float(row["value"]) for row in item["months"]}
    period = item["months"][-1]["period"]
    year, month = map(int, period.split("-"))
    previous = f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"
    year_ago = f"{year - 1:04d}-{month:02d}"
    current = values[period]

    def growth(prior: str) -> float | None:
        before = values.get(prior)
        return round((current / before - 1) * 100, 1) if before and before > 0 else None

    cutoff = _utc(as_of).date()
    age_days = (cutoff - dt.date(year, month, 1)).days
    return {**base, "status": "stale" if age_days > 105 else "ready",
            "reason": "원천 월간 자료가 오래됐습니다. 다음 공표를 기다립니다." if age_days > 105 else None,
            "period": period, "index": round(current, 4),
            "mom_pct": growth(previous), "yoy_pct": growth(year_ago),
            "age_days": age_days, "observed_at": item["observed_at"],
            "available_at": item["available_at"], "source_last_modified": item["source_last_modified"],
            "source_published_at": None, "raw_sha256": item["raw_sha256"]}


def _open():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    req = urllib.request.Request(SOURCE_URL, headers={
        "User-Agent": "signal-desk-industry-research/1.0", "Accept-Encoding": "identity"})
    return urllib.request.build_opener(NoRedirect()).open(req, timeout=25)


def collect(path: Path) -> dict:
    stage = "request"
    response_bytes = 0
    try:
        with _open() as response:
            raw = response.read(MAX_BYTES + 1)
            modified = response.headers.get("Last-Modified")
        response_bytes = len(raw)
        stage = "validate_and_archive"
        return archive(path, raw, observed_at=_now(), source_last_modified=modified)
    except Exception as exc:
        return {"status": "collection_failed", "failure_stage": stage,
                "failure_type": type(exc).__name__, "response_bytes": response_bytes}


def refresh(path: Path, *, now: dt.datetime,
            state_get: Callable[[str], object], state_set: Callable[[str, object], None],
            collector: Callable[[Path], dict] = collect) -> dict:
    """One weekly attempt; durable four-request monthly ceiling across restarts."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone required")
    key = "fed_g17_attempt"
    prior = state_get(key)
    if isinstance(prior, dict) and prior.get("at") is not None:
        try:
            age = now.timestamp() - float(prior["at"])
        except (TypeError, ValueError):
            return {"status": "invalid_attempt_state", "requested": 0}
        if age < 7 * 86400:
            return {"status": "not_due", "requested": 0}
    budget_key = f"fed_g17_requests:{now.strftime('%Y-%m')}"
    try:
        used = int(state_get(budget_key) or 0)
    except (TypeError, ValueError):
        used = MAX_REQUESTS_PER_MONTH
    if used < 0 or used >= MAX_REQUESTS_PER_MONTH:
        return {"status": "budget_exhausted", "requested": 0}
    stamp = {"at": int(now.timestamp()), "status": "started"}
    state_set(budget_key, used + 1)
    state_set(key, stamp)
    result = collector(path)
    status = str(result.get("status") or "collection_failed")
    state_set(key, {**stamp, "status": status})
    return {"requested": 1, **result, "at": now.isoformat()}
