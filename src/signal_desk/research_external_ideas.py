"""Research-only, versioned external ideas supplied by a human.

This separate SQLite file has no reader in the KB, signal, bot, or live-order
paths. URLs are metadata; this module never fetches them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DB = Path("data/research/external_ideas.db")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS idea_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    post_key TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    author TEXT NOT NULL,
    aggregator_url TEXT,
    source_published_at TEXT,
    first_observed_at TEXT NOT NULL,
    revision_observed_at TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    instrument_kind TEXT NOT NULL,
    claim_kind TEXT NOT NULL,
    stance TEXT NOT NULL,
    source_excerpt TEXT NOT NULL,
    thesis TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    horizon TEXT NOT NULL,
    evidence_url TEXT,
    redacted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_key, revision),
    UNIQUE(source_key, record_sha256)
);
CREATE INDEX IF NOT EXISTS idea_versions_recent ON idea_versions(id DESC);
"""


@contextmanager
def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _url(value: str | None, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("source URL is required")
        return None
    if not isinstance(value, str) or len(value) > 800:
        raise ValueError("invalid URL")
    if any(char in value for char in "\r\n\t"):
        raise ValueError("invalid URL")
    parts = urlsplit(value.strip())
    if (parts.scheme.lower() != "https" or not parts.hostname or parts.username
            or parts.password or parts.port or not re.fullmatch(r"[a-zA-Z0-9.-]+", parts.hostname)):
        raise ValueError("only public HTTPS URLs are accepted")
    host = parts.hostname.lower()
    if host == "twitter.com":
        host = "x.com"
    if host == "www.reddit.com":
        host = "reddit.com"
    query = "" if host in ("x.com", "reddit.com") else parts.query
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def _text(value: str | None, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("text field must be a string")
    result = value.strip()
    if len(result) > limit or (required and not result):
        raise ValueError("text field empty or too long")
    return result


def _published_at(value: str | None, now: dt.datetime) -> str | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("publication time must be ISO with timezone")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("publication time must be ISO with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed > now:
        raise ValueError("publication time must be observed, timezone-aware and not future")
    return parsed.astimezone(dt.timezone.utc).isoformat()


def add_manual(data: dict) -> dict:
    """Append one human-reviewed version; identical replays are idempotent."""
    if not isinstance(data, dict):
        raise ValueError("idea must be an object")
    now = _now()
    source_url = _url(data.get("source_url"), required=True)
    author = _text(data.get("author"), 100, required=True)
    market = _text(data.get("market"), 2, required=True).upper()
    ticker = _text(data.get("ticker"), 12, required=True).upper()
    if market == "KR":
        valid_ticker = bool(re.fullmatch(r"[0-9]{6}", ticker))
    elif market == "US":
        valid_ticker = bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", ticker))
    else:
        raise ValueError("market must be KR or US")
    if not valid_ticker:
        raise ValueError("ticker and market do not match")
    instrument = _text(data.get("instrument_kind") or "unknown", 12)
    claim_kind = _text(data.get("claim_kind") or "unknown", 12)
    claim_key = _text(data.get("claim_key") or ticker, 80, required=True)
    stance = _text(data.get("stance") or "unclear", 12)
    horizon = _text(data.get("horizon") or "unknown", 12)
    if (instrument not in ("stock", "options", "unknown")
            or claim_kind not in ("mention", "thesis", "intent", "holding", "unknown")
            or stance not in ("bullish", "bearish", "unclear")
            or horizon not in ("intraday", "days", "months", "unknown")):
        raise ValueError("invalid instrument, stance or horizon")
    fields = {
        "source_url": source_url, "author": author,
        "aggregator_url": _url(data.get("aggregator_url")),
        "source_published_at": _published_at(data.get("source_published_at"), now),
        "market": market, "ticker": ticker, "instrument_kind": instrument,
        "claim_kind": claim_kind, "claim_key": claim_key,
        "stance": stance, "source_excerpt": _text(data.get("source_excerpt"), 240),
        "thesis": _text(data.get("thesis"), 500, required=True),
        "invalidation": _text(data.get("invalidation"), 300),
        "horizon": horizon, "evidence_url": _url(data.get("evidence_url")),
    }
    domain = urlsplit(source_url).hostname or ""
    fields["source_platform"] = "x" if domain == "x.com" else (
        "reddit" if domain == "reddit.com" or domain.endswith(".reddit.com") else "other")
    post_key = hashlib.sha256(source_url.encode()).hexdigest()
    source_key = hashlib.sha256(f"{post_key}|{market}|{ticker}|{claim_key}".encode()).hexdigest()
    content = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    record_sha = hashlib.sha256(content.encode()).hexdigest()
    observed = now.isoformat()
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute(
            "SELECT * FROM idea_versions WHERE source_key=? ORDER BY revision DESC LIMIT 1",
            (source_key,)).fetchone()
        if previous and (previous["author"] != author or previous["redacted"]):
            raise ValueError("source author differs or this source was redacted")
        duplicate = con.execute(
            "SELECT id, revision FROM idea_versions WHERE source_key=? AND record_sha256=?",
            (source_key, record_sha)).fetchone()
        if duplicate:
            return {"source_key": source_key, "post_key": post_key,
                    "revision": duplicate["revision"],
                    "id": duplicate["id"], "added": False}
        revision = 1 if previous is None else previous["revision"] + 1
        first_observed = observed if previous is None else previous["first_observed_at"]
        con.execute("""INSERT INTO idea_versions
            (source_key, post_key, claim_key, revision, source_url, source_platform, author, aggregator_url,
             source_published_at, first_observed_at, revision_observed_at, record_sha256,
             market, ticker, instrument_kind, claim_kind, stance, source_excerpt, thesis,
             invalidation, horizon, evidence_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_key, post_key, claim_key, revision, fields["source_url"], fields["source_platform"], author,
             fields["aggregator_url"], fields["source_published_at"], first_observed,
             observed, record_sha, market, ticker, instrument, claim_kind, stance,
             fields["source_excerpt"], fields["thesis"], fields["invalidation"],
             horizon, fields["evidence_url"]))
        return {"source_key": source_key, "post_key": post_key, "revision": revision,
                "id": con.execute("SELECT last_insert_rowid()").fetchone()[0],
                "added": True}


def list_latest(limit: int = 50) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("invalid list limit")
    with _conn() as con:
        rows = con.execute("""SELECT v.* FROM idea_versions v JOIN
            (SELECT source_key, MAX(revision) AS revision FROM idea_versions GROUP BY source_key) latest
            ON latest.source_key=v.source_key AND latest.revision=v.revision
            WHERE v.redacted=0 ORDER BY v.id DESC LIMIT ?""", (limit,)).fetchall()
    return [_present(row) for row in rows]


def revisions(source_key: str) -> list[dict]:
    if not isinstance(source_key, str) or not re.fullmatch(r"[0-9a-f]{64}", source_key):
        raise ValueError("invalid source key")
    with _conn() as con:
        rows = con.execute("SELECT * FROM idea_versions WHERE source_key=? ORDER BY revision",
                           (source_key,)).fetchall()
    return [_present(row) for row in rows]


def redact(source_key: str) -> int:
    """Erase limited copied text if retention rights require removal."""
    if not isinstance(source_key, str) or not re.fullmatch(r"[0-9a-f]{64}", source_key):
        raise ValueError("invalid source key")
    with _conn() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute("""UPDATE idea_versions
            SET source_excerpt='', thesis='', invalidation='', redacted=1
            WHERE source_key=? AND redacted=0""", (source_key,)).rowcount
    return changed


def _present(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["redacted"] = bool(out["redacted"])
    out["research_only"] = True
    out["live_order_eligible"] = False
    out["registered_verdict"] = False
    out["historical_reconstruction_eligible"] = False
    out["forward_available_at"] = out["revision_observed_at"]
    out["source_stance_verified"] = False
    out["stance_attribution"] = "user_interpretation"
    out["original_post_edit_verified"] = False
    out["evidence_status"] = "linked_unreviewed" if out["evidence_url"] else "none"
    return out
