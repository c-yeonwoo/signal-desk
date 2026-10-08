"""Public SIA monthly semiconductor-sales releases, kept out of trading inputs.

Only compact facts stated in the public press release are retained. The paid
WSTS/SIA dataset and full press-release text are never downloaded or archived.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

API_URL = "https://www.semiconductors.org/wp-json/wp/v2/posts"
SOURCE_PAGE = "https://www.semiconductors.org/data-resources/market-data/"
SOURCE_HOST = "www.semiconductors.org"
SALES_CATEGORY = 3
SCHEMA = "sia-public-monthly-sales-v1"
DEFAULT_ARCHIVE = Path("data/raw/sia-market.db")
MAX_BYTES = 1024 * 1024
MAX_REQUESTS_PER_MONTH = 12
POLL_INTERVAL = dt.timedelta(days=3)
_MONTHS = "January February March April May June July August September October November December".split()
_MONTH_RE = "|".join(_MONTHS)
_FACT_RE = re.compile(
    rf"global semiconductor sales were\s+\$(?P<current>[\d,.]+)\s+billion\s+"
    rf"during the month of (?P<month>{_MONTH_RE}) (?P<year>20\d{{2}}),\s+"
    rf"an increase of (?P<mom>[\d,.]+)%\s+compared to the (?P<prev_month>{_MONTH_RE})\s+"
    rf"(?P<year2>20\d{{2}}) total of \$(?P<prev>[\d,.]+)\s+billion\s+"
    rf"and (?P<yoy>[\d,.]+)% more than the (?P<year_month>{_MONTH_RE})\s+"
    rf"(?P<year3>20\d{{2}}) total of \$(?P<year_ago>[\d,.]+)\s+billion",
    re.IGNORECASE,
)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _utc(value: str | dt.datetime) -> dt.datetime:
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def parse(raw: bytes, *, observed_at: dt.datetime) -> list[dict]:
    """Validate public post metadata and extract only release-stated totals."""
    if len(raw) > MAX_BYTES:
        raise ValueError("SIA response too large")
    body = json.loads(raw)
    if not isinstance(body, list):
        raise ValueError("SIA response shape invalid")
    observed = _utc(observed_at)
    found: list[dict] = []
    for post in body:
        if not isinstance(post, dict) or SALES_CATEGORY not in post.get("categories", []):
            continue
        title = _text(str((post.get("title") or {}).get("rendered") or ""))
        link = str(post.get("link") or "")
        parsed_link = urllib.parse.urlparse(link)
        if (parsed_link.scheme != "https" or parsed_link.hostname != SOURCE_HOST
                or parsed_link.path.strip("/") != str(post.get("slug") or "").strip("/")):
            continue
        if "semiconductor" not in title.lower() or "sales" not in title.lower():
            continue
        published = _utc(str(post.get("date_gmt") or ""))
        if published > observed:
            continue
        markup = str((post.get("content") or {}).get("rendered") or "")
        text = _text(markup)
        match = _FACT_RE.search(text)
        if not match or not re.search(r"three[- ]month moving average", text, re.IGNORECASE):
            continue
        month = match.group("month").title()
        year = int(match.group("year"))
        month_index = _MONTHS.index(month)
        previous_month = _MONTHS[(month_index - 1) % 12]
        previous_year = year if month_index else year - 1
        if (match.group("prev_month").title() != previous_month
                or int(match.group("year2")) != previous_year
                or match.group("year_month").title() != month
                or int(match.group("year3")) != year - 1):
            continue

        def number(key: str) -> float:
            value = float(match.group(key).replace(",", ""))
            if not (0 < value < 1_000_000):
                raise ValueError("SIA fact outside expected range")
            return value

        content_hash = hashlib.sha256(markup.encode("utf-8")).hexdigest()
        found.append({
            "post_id": int(post["id"]), "title": title, "source_url": link,
            "published_at": published.isoformat(), "period": f"{year:04d}-{month_index + 1:02d}",
            "sales_usd_billions": number("current"), "mom_pct": number("mom"),
            "prior_month_sales_usd_billions": number("prev"), "yoy_pct": number("yoy"),
            "year_ago_sales_usd_billions": number("year_ago"),
            "measure": "3개월 이동평균", "source_content_sha256": content_hash,
        })
    return sorted(found, key=lambda row: (row["published_at"], row["post_id"]), reverse=True)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS sia_market_observations "
                 "(id TEXT PRIMARY KEY, available_at TEXT NOT NULL, envelope BLOB NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS sia_market_available "
                 "ON sia_market_observations(available_at)")
    return conn


def archive(path: Path, facts: list[dict], *, observed_at: dt.datetime) -> dict:
    observed = _utc(observed_at)
    inserted = 0
    ids: list[str] = []
    # The public feed includes older releases. This card intentionally tracks
    # only the newest release, not a retroactive historical backfill.
    facts = sorted(facts, key=lambda item: (item["published_at"], item["post_id"]), reverse=True)[:1]
    conn = _connect(path)
    try:
        with conn:
            for fact in facts:
                if _utc(fact["published_at"]) > observed:
                    continue
                envelope = {"schema": SCHEMA, "observed_at": observed.isoformat(),
                            "available_at": observed.isoformat(), "fact": fact,
                            "strict_pit_eligible": False, "live_eligible": False}
                material = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
                identity = json.dumps({"schema": SCHEMA, "fact": fact}, sort_keys=True,
                                      separators=(",", ":")).encode()
                # One ID per public release version, not one row per poll.
                identifier = hashlib.sha256(identity).hexdigest()
                was_inserted = conn.execute(
                    "INSERT OR IGNORE INTO sia_market_observations VALUES (?,?,?)",
                    (identifier, observed.isoformat(), material)).rowcount
                inserted += was_inserted
                ids.append(identifier)
    finally:
        conn.close()
    return {"status": "ok" if facts else "no_data", "requested": 1,
            "ok": 1 if facts else 0, "failed": 0 if facts else 1,
            "response_bytes": 0, "raw_changed": inserted,
            "observations_added": inserted, "ids": ids}


def _open():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    query = urllib.parse.urlencode({
        "categories": SALES_CATEGORY, "per_page": 10,
        "_fields": "id,date_gmt,slug,link,title,categories,content",
    })
    request = urllib.request.Request(f"{API_URL}?{query}", headers={
        "User-Agent": "SignalDesk/1.0 (public industry release monitor)",
        "Accept": "application/json", "Accept-Encoding": "identity",
    })
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=20)


def collect(path: Path, *, now: dt.datetime) -> dict:
    stage, response_bytes = "request", 0
    try:
        with _open() as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != SOURCE_HOST:
                raise ValueError("SIA response host invalid")
            raw = response.read(MAX_BYTES + 1)
        response_bytes = len(raw)
        stage = "validate_and_archive"
        facts = parse(raw, observed_at=now)
        result = archive(path, facts, observed_at=now)
        result["response_bytes"] = response_bytes
        return result
    except Exception as exc:
        return {"status": "collection_failed", "failure_stage": stage,
                "failure_type": type(exc).__name__, "requested": 1, "ok": 0,
                "failed": 1, "response_bytes": response_bytes, "raw_changed": 0}


def refresh(path: Path, *, now: dt.datetime,
            state_get: Callable[[str], object], state_set: Callable[[str, object], None],
            reserve: Callable[[str], bool], collector: Callable[..., dict] = collect) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone required")
    try:
        prior = state_get("sia_market_attempt")
    except Exception:
        return {"status": "state_failure", "requested": 0}
    if isinstance(prior, dict) and prior.get("at") is not None:
        try:
            if now.timestamp() - float(prior["at"]) < POLL_INTERVAL.total_seconds():
                return {"status": "not_due", "requested": 0}
        except (TypeError, ValueError):
            return {"status": "invalid_attempt_state", "requested": 0}
    budget_key = f"sia_market_requests:{now:%Y-%m}"
    try:
        if not reserve(budget_key):
            return {"status": "budget_exhausted", "requested": 0}
        state_set("sia_market_attempt", {"at": int(now.timestamp()), "status": "started"})
    except Exception:
        return {"status": "state_failure", "requested": 0}
    result = collector(path, now=now)
    result.setdefault("requested", 1)
    result["at"] = now.isoformat()
    try:
        state_set("sia_market_attempt", {"at": int(now.timestamp()), "status": result.get("status")})
    except Exception:
        result["status"] = "state_failure"
    return result


def latest(path: Path, *, as_of: dt.datetime) -> dict | None:
    if not path.exists():
        return None
    cutoff = _utc(as_of).isoformat()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id,envelope,available_at FROM sia_market_observations "
                           "WHERE available_at<=? ORDER BY available_at DESC,rowid DESC LIMIT 1",
                           (cutoff,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    identifier, material, available_at = row
    envelope = json.loads(material)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("fact"), dict):
        raise ValueError("SIA archive integrity failure")
    identity = json.dumps({"schema": SCHEMA, "fact": envelope.get("fact")}, sort_keys=True,
                          separators=(",", ":")).encode()
    if (hashlib.sha256(identity).hexdigest() != identifier or envelope.get("schema") != SCHEMA
            or envelope.get("available_at") != available_at
            or envelope.get("strict_pit_eligible") is not False
            or envelope.get("live_eligible") is not False):
        raise ValueError("SIA archive integrity failure")
    return {"id": identifier, **envelope}


def describe(path: Path, *, as_of: dt.datetime) -> dict:
    base = {"source": "SIA 공개 발표", "source_url": SOURCE_PAGE,
            "scope": "글로벌 반도체 매출(출하액) · 3개월 이동평균",
            "mode": "read_only_research", "strict_pit_eligible": False,
            "live_eligible": False,
            "note": "세계 산업 매출 흐름입니다. 한국 특정 기업의 실적이나 매매 신호를 뜻하지 않습니다."}
    try:
        item = latest(path, as_of=as_of)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {**base, "status": "archive_error", "reason": "보존 자료를 확인할 수 없습니다."}
    if not item:
        return {**base, "status": "not_recorded", "reason": "아직 공개 발표를 관측하지 않았습니다."}
    fact = item["fact"]
    published = _utc(fact["published_at"])
    age_days = (_utc(as_of) - published).days
    return {**base, "status": "stale" if age_days > 60 else "ready",
            "reason": "새 발표 확인이 늦어지고 있습니다." if age_days > 60 else None,
            **fact, "observed_at": item["observed_at"], "available_at": item["available_at"],
            "age_days": age_days}
