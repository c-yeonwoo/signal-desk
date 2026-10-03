"""First-observed SEC ticker/CIK associations, never a historical PIT mapping."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import zlib
from pathlib import Path

from signal_desk.ingest import edgar

SOURCE_URL = "https://www.sec.gov/files/company_tickers.json"
SOURCE_PAGE = "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"
SCHEMA = "sec-ticker-cik-observation-v1"
DEFAULT_ARCHIVE = Path("data/raw/sec-issuer-map.db")
MAX_BYTES = 2 * 1024 * 1024
MIN_VALID_ROWS = 1000


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(dt.timezone.utc)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse(raw: bytes, *, min_rows: int = MIN_VALID_ROWS) -> tuple[dict[str, dict], list[str]]:
    if len(raw) > MAX_BYTES:
        raise ValueError("SEC ticker map too large")
    body = json.loads(raw)
    if not isinstance(body, dict) or len(body) < min_rows:
        raise ValueError("SEC ticker map missing rows")
    mapping: dict[str, dict] = {}
    ambiguous: set[str] = set()
    valid = 0
    for row in body.values():
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        name = row.get("title")
        if not isinstance(ticker, str) or not isinstance(name, str):
            continue
        ticker = ticker.upper()
        name = name.strip()
        raw_cik = row.get("cik_str")
        if (isinstance(raw_cik, bool) or not isinstance(raw_cik, (int, str))
                or not re.fullmatch(r"[0-9]{1,10}", str(raw_cik))):
            continue
        cik = int(raw_cik)
        if not re.fullmatch(r"[A-Z0-9.-]{1,10}", ticker) or not name or not 0 < cik < 10**10:
            continue
        valid += 1
        value = {"cik": f"{cik:010d}", "name": name}
        if ticker in mapping and mapping[ticker]["cik"] != value["cik"]:
            ambiguous.add(ticker)
        else:
            mapping[ticker] = value
    if valid < min_rows:
        raise ValueError("SEC ticker map valid rows insufficient")
    for ticker in ambiguous:
        mapping.pop(ticker, None)
    return mapping, sorted(ambiguous)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS sec_issuer_map_observations "
                 "(id TEXT PRIMARY KEY, available_at TEXT NOT NULL, envelope BLOB NOT NULL, raw_zlib BLOB NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS sec_map_available ON sec_issuer_map_observations(available_at)")
    return conn


def archive(path: Path, raw: bytes, *, observed_at: str) -> dict:
    observed = _utc(observed_at)
    mapping, ambiguous = parse(raw)
    available = max(observed, _utc(_now())).isoformat()
    envelope = {"schema": SCHEMA, "source_url": SOURCE_URL,
                "observed_at": observed.isoformat(), "available_at": available,
                "source_published_at": None, "strict_pit_eligible": False,
                "live_eligible": False, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "mapping": mapping, "ambiguous": ambiguous}
    material = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    identifier = hashlib.sha256(material).hexdigest()
    conn = _connect(path)
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO sec_issuer_map_observations VALUES (?,?,?,?)",
                         (identifier, available, material, zlib.compress(raw, level=6)))
    finally:
        conn.close()
    return {"status": "ok", "id": identifier, "tickers": len(mapping),
            "ambiguous": len(ambiguous), "response_bytes": len(raw), "available_at": available}


def latest(path: Path, *, as_of: str) -> dict | None:
    cutoff = _utc(as_of).isoformat()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id, envelope, raw_zlib, available_at FROM sec_issuer_map_observations "
                           "WHERE available_at<=? ORDER BY available_at DESC, rowid DESC LIMIT 1",
                           (cutoff,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    identifier, material, compressed, available = row
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not inflater.eof or inflater.unused_data:
            raise ValueError("SEC map raw invalid")
        envelope = json.loads(material)
    except (zlib.error, ValueError, TypeError) as exc:
        raise ValueError("SEC map archive integrity failure") from exc
    if (not isinstance(envelope, dict) or not isinstance(envelope.get("mapping"), dict)
            or not isinstance(envelope.get("ambiguous"), list)
            or hashlib.sha256(material).hexdigest() != identifier
            or hashlib.sha256(raw).hexdigest() != envelope.get("raw_sha256")
            or envelope.get("available_at") != available
            or envelope.get("source_url") != SOURCE_URL or envelope.get("schema") != SCHEMA):
        raise ValueError("SEC map archive integrity failure")
    return {"id": identifier, **envelope}


def lookup(path: Path, *, ticker: str, as_of: str) -> dict:
    """Abstain when an observed association is missing, ambiguous, or stale."""
    base = {"ticker": ticker, "source": "SEC", "source_url": SOURCE_PAGE,
            "strict_pit_eligible": False, "live_eligible": False}
    if not re.fullmatch(r"[A-Z0-9.-]{1,10}", ticker):
        return {**base, "status": "invalid_ticker"}
    try:
        item = latest(path, as_of=as_of)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return {**base, "status": "archive_error"}
    if item is None:
        return {**base, "status": "not_recorded"}
    try:
        age = _utc(as_of) - _utc(item["available_at"])
        if age < dt.timedelta(0) or age > dt.timedelta(days=30):
            return {**base, "status": "stale", "observed_at": item["observed_at"]}
        if ticker in item["ambiguous"]:
            return {**base, "status": "ambiguous", "observed_at": item["observed_at"]}
        company = item["mapping"].get(ticker)
        if company is None:
            return {**base, "status": "unmapped", "observed_at": item["observed_at"]}
        if (not isinstance(company, dict) or not re.fullmatch(r"[0-9]{10}", company.get("cik", ""))
                or not isinstance(company.get("name"), str)):
            raise ValueError("invalid company identity")
        return {**base, "status": "mapped", "cik": company["cik"], "name": company["name"],
                "observed_at": item["observed_at"], "map_id": item["id"]}
    except (KeyError, TypeError, ValueError):
        return {**base, "status": "archive_error"}


def collect(path: Path) -> dict:
    """One official request with the shared SEC identity/rate guard."""
    if not edgar.available():
        return {"status": "missing_contact", "requested": 0}
    raw = edgar._get(SOURCE_URL)
    if raw is None:
        return {"status": "collection_failed", "requested": 1, "response_bytes": 0}
    try:
        return {"requested": 1, **archive(path, raw, observed_at=_now())}
    except (ValueError, TypeError, sqlite3.Error, OSError):
        return {"status": "collection_failed", "requested": 1, "response_bytes": len(raw)}
