"""Opt-in official financial evidence intake, isolated from scoring and orders.

One explicit issuer/request at a time; no scheduler, backfill, LLM or automatic
promotion. Filing dates are NOT intraday publication times. Observing old filings
today never makes them historical PIT inputs. Raw API responses are retained;
they are not substitutes for the original filing documents linked by each fact.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "financial-evidence-v1"
MAX_BYTES = 16 * 1024 * 1024
REPORTS = {"11013", "11012", "11014", "11011"}
# Explicit concepts only. Retain taxonomy/tag; do not silently combine aliases.
CONCEPTS = {
    "us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "OperatingIncomeLoss", "NetIncomeLoss",
                "NetCashProvidedByUsedInOperatingActivities", "InventoryNet",
                "AccountsReceivableNetCurrent"},
    "ifrs-full": {"Revenue", "ProfitLossFromOperatingActivities", "ProfitLoss",
                  "CashFlowsFromUsedInOperatingActivities", "Inventories",
                  "TradeAndOtherCurrentReceivables"},
}


@dataclass(frozen=True)
class Target:
    source: str
    issuer: str  # SEC CIK or DART corp_code, never a guessed ticker mapping
    year: str = ""
    report: str = ""
    basis: str = "CFS"

    def __post_init__(self):
        if self.source == "sec":
            if not re.fullmatch(r"[0-9]{10}", self.issuer) or int(self.issuer) == 0:
                raise ValueError("SEC CIK must have 10 digits")
            if self.year or self.report:
                raise ValueError("SEC companyfacts does not accept year/report filters")
        elif self.source == "dart":
            if not re.fullmatch(r"[0-9]{8}", self.issuer):
                raise ValueError("DART corp_code must have 8 digits")
            if not re.fullmatch(r"[0-9]{4}", self.year) or int(self.year) < 2015:
                raise ValueError("DART business year must be >= 2015")
            if self.report not in REPORTS or self.basis not in {"CFS", "OFS"}:
                raise ValueError("explicit DART report and CFS/OFS basis required")
        else:
            raise ValueError("only sec and dart are supported")

    @property
    def url(self) -> str:
        if self.source == "sec":
            return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{self.issuer}.json"
        return "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?" + urllib.parse.urlencode({
            "corp_code": self.issuer, "bsns_year": self.year,
            "reprt_code": self.report, "fs_div": self.basis})


def _number(value) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return str(number) if number.is_finite() else None
    except InvalidOperation:
        return None


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(target: Target, body: dict) -> tuple[str, list[dict]]:
    """Preserve units, scope, periods and revisions; calculate no growth or score."""
    rows = []
    if target.source == "dart":
        if body.get("status") == "013":
            return "no_data", []
        if body.get("status") != "000" or not isinstance(body.get("list"), list):
            raise ValueError("DART response not successful")
        for item in body["list"]:
            if (item.get("corp_code") != target.issuer or item.get("bsns_year") != target.year
                    or item.get("reprt_code") != target.report):
                raise ValueError("DART response identity mismatch")
            if item.get("fs_div", target.basis) != target.basis:
                raise ValueError("DART statement basis mismatch")
            accession = str(item.get("rcept_no", ""))
            if not re.fullmatch(r"[0-9]{14}", accession):
                raise ValueError("DART filing identifier missing")
            statement = item.get("sj_div")
            if statement not in {"BS", "IS", "CIS", "CF"}:
                continue  # SCE dimensional rows are not comparable totals
            quarter_income = statement in {"IS", "CIS"} and target.report != "11011"
            # Do not infer calendar period boundaries from a fiscal-year label.
            for field, kind in (("thstrm_amount", "instant" if statement == "BS" else
                                 "quarter" if quarter_income else "reported_duration"),
                                ("thstrm_add_amount", "year_to_date")):
                if field == "thstrm_add_amount" and statement == "BS":
                    continue
                value = _number(item.get(field))
                if value is None:
                    continue
                rows.append({"concept": item.get("account_id"), "label": item.get("account_nm"),
                             "statement": statement, "basis": target.basis,
                             "unit": item.get("currency") or None, "value": value,
                             "amount_field": field, "period_kind": kind,
                             "period_start": None, "period_end": None,
                             "period_label": item.get("thstrm_nm"), "business_year": target.year,
                             "report": target.report, "accession": accession,
                             "filing_date": None, "source_published_at": None,
                             "filing_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={accession}"})
    else:
        if str(body.get("cik", "")).zfill(10) != target.issuer or not isinstance(body.get("facts"), dict):
            raise ValueError("SEC response identity/schema mismatch")
        for taxonomy, concepts in CONCEPTS.items():
            for concept in sorted(concepts):
                fact = body["facts"].get(taxonomy, {}).get(concept, {})
                for unit, entries in fact.get("units", {}).items():
                    for item in entries:
                        if item.get("form") not in {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "6-K", "6-K/A"}:
                            continue
                        value = _number(item.get("val"))
                        if value is None:
                            continue
                        accession = str(item.get("accn", ""))
                        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
                            raise ValueError("SEC filing identifier missing")
                        end = date.fromisoformat(item["end"])
                        start = date.fromisoformat(item["start"]) if item.get("start") else None
                        filed = date.fromisoformat(item["filed"])
                        if (start and start > end) or filed < end:
                            raise ValueError("invalid SEC period")
                        # fp/fy describe the filing, not necessarily this fact's period.
                        days = (end - start).days + 1 if start else None
                        instant = concept in {"InventoryNet", "AccountsReceivableNetCurrent",
                                              "Inventories", "TradeAndOtherCurrentReceivables"}
                        # Length is a screening hint, not a certified fiscal quarter.
                        kind = ("instant" if instant and not start else "unknown" if not start else
                                "quarter_length" if 70 <= days <= 105 else "reported_duration")
                        rows.append({"concept": f"{taxonomy}:{concept}", "label": fact.get("label"),
                                     "unit": unit, "value": value, "period_kind": kind,
                                     "period_start": start.isoformat() if start else None,
                                     "period_end": end.isoformat(), "duration_days": days,
                                     "accession": accession, "filing_date": filed.isoformat(),
                                     "source_published_at": None, "form": item["form"],
                                     "filing_url": f"https://www.sec.gov/Archives/edgar/data/{int(target.issuer)}/{accession.replace('-', '')}/{accession}-index.html"})
    return ("ok" if rows else "no_supported_facts"), rows


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS financial_observations "
                 "(id TEXT PRIMARY KEY, source_url TEXT NOT NULL, observed_at TEXT NOT NULL, "
                 "available_at TEXT NOT NULL, "
                 "envelope BLOB NOT NULL, raw BLOB NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS financial_observations_lookup "
                 "ON financial_observations(source_url, available_at)")
    return conn


def archive(path: Path, target: Target, raw: bytes, *, observed_at: str) -> dict:
    """Append an observation atomically. Production callers stamp time after retrieval."""
    if len(raw) > MAX_BYTES:
        raise ValueError("response too large")
    body = json.loads(raw, parse_float=Decimal)
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    status, facts = normalize(target, body)
    observed = _utc(observed_at)
    # A downloaded response is not a parsed, available fact yet. Historical
    # imports get today's availability, even if a caller supplies an old time.
    available = max(observed, _utc(_now()))
    envelope = {"schema": SCHEMA, "source": target.source, "issuer": target.issuer,
                "source_url": target.url, "observed_at": observed, "available_at": available,
                "status": status,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "facts": facts,
                "source_available_at_verified": False, "strict_pit_eligible": False,
                "live_eligible": False}
    material = _json(envelope)
    identifier = hashlib.sha256(material).hexdigest()
    conn = _connect(path)
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO financial_observations VALUES (?,?,?,?,?,?)",
                         (identifier, target.url, observed, available, material, raw))
    finally:
        conn.close()
    return {"id": identifier, "status": status, "facts": len(facts), "observed_at": observed,
            "available_at": available,
            "strict_pit_eligible": False, "live_eligible": False}


def latest(path: Path, target: Target, *, as_of: str) -> dict | None:
    """Last actually observed version, including no-data (never resurrect old data)."""
    cutoff = _utc(as_of)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT id, envelope, raw, observed_at, available_at FROM financial_observations "
                           "WHERE source_url=? AND available_at<=? ORDER BY available_at DESC, rowid DESC LIMIT 1",
                           (target.url, cutoff)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    identifier, material, raw, observed, available = row
    envelope = json.loads(material)
    if (hashlib.sha256(material).hexdigest() != identifier or
            hashlib.sha256(raw).hexdigest() != envelope["raw_sha256"] or
            envelope["schema"] != SCHEMA or envelope["source_url"] != target.url or
            envelope["observed_at"] != observed or envelope["available_at"] != available):
        raise ValueError("financial observation integrity failure")
    return {"id": identifier, **envelope}


def dart_targets(path: Path, issuer: str, *, as_of: str) -> list[Target]:
    """Enumerate actually observed DART report requests for one issuer.

    No file creation or network access. A target with a later no-data response
    remains visible so callers can represent that state explicitly.
    """
    if not re.fullmatch(r"[0-9]{8}", issuer):
        raise ValueError("invalid DART corp_code")
    cutoff = _utc(as_of)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        urls = [row[0] for row in conn.execute(
            "SELECT DISTINCT source_url FROM financial_observations WHERE available_at<=? "
            "AND source_url LIKE 'https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?%'",
            (cutoff,))]
    finally:
        conn.close()
    targets = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "opendart.fss.or.kr" or parsed.path != "/api/fnlttSinglAcntAll.json":
            continue
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("corp_code") != [issuer]:
            continue
        try:
            target = Target("dart", issuer, params["bsns_year"][0], params["reprt_code"][0],
                            params["fs_div"][0])
        except (ValueError, KeyError, IndexError):
            continue
        if target.url == url:
            targets.append(target)
    return sorted(targets, key=lambda target: (target.year,
                    {"11013": 1, "11012": 2, "11014": 3, "11011": 4}[target.report]), reverse=True)


def collect(path: Path, target: Target, *, dart_key: str = "", sec_contact: str = "") -> dict:
    """One official request, bounded response, no retries/fallback or key persistence."""
    url = target.url
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if target.source == "dart":
        if not dart_key:
            return {"status": "missing_credentials", "facts": 0}
        url += "&" + urllib.parse.urlencode({"crtfc_key": dart_key})
    else:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sec_contact):
            return {"status": "missing_contact", "facts": 0}
        headers["User-Agent"] = f"signal-desk financial-research ({sec_contact})"
    stage = "request"
    response_bytes = 0
    try:
        # No redirect may forward credentials to another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        response_bytes = len(raw)
        observed = _now()
        stage = "validate_and_archive"
        return {**archive(path, target, raw, observed_at=observed), "response_bytes": response_bytes}
    except Exception:
        # urllib exceptions may contain a URL with the key. Never log/return them.
        return {"status": "collection_failed", "failure_stage": stage,
                "facts": 0, "response_bytes": response_bytes}
