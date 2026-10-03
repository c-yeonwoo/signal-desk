"""Independent raw-response checks for observed DART watchlist cards.

This audits persisted bytes only. It does not fetch filings, score stocks,
certify source publication time, or prove that the API provider was correct.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
from decimal import Decimal, InvalidOperation
from pathlib import Path

from signal_desk.ingest import financial_evidence as evidence
from signal_desk.signals import financial_change

VERSION = "dart-card-raw-audit-v1"
_SPECS = {"revenue": ("ifrs-full_Revenue", "IS"),
          "operating_income": ("dart_OperatingIncomeLoss", "IS"),
          "operating_cash_flow": ("ifrs-full_CashFlowsFromUsedInOperatingActivities", "CF"),
          "inventory": ("ifrs-full_Inventories", "BS")}


def _raw(path: Path, target: evidence.Target, expected_id: str, *, as_of: str) -> dict:
    archived = evidence.latest(path, target, as_of=as_of)
    if not archived or archived["id"] != expected_id or archived["status"] != "ok":
        raise ValueError("observation identity changed")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT raw FROM financial_observations WHERE id=? AND source_url=?",
                           (expected_id, target.url)).fetchone()
    finally:
        conn.close()
    if not row or hashlib.sha256(row[0]).hexdigest() != archived["raw_sha256"]:
        raise ValueError("raw response integrity failure")
    body = json.loads(row[0])
    if not isinstance(body, dict) or body.get("status") != "000" or not isinstance(body.get("list"), list):
        raise ValueError("invalid raw DART response")
    return body


def _amount(raw: dict, target: evidence.Target, *, concept: str, statement: str,
            accession: str, unit: str) -> Decimal:
    matches = [row for row in raw["list"] if isinstance(row, dict)
               and row.get("corp_code") == target.issuer
               and row.get("bsns_year") == target.year
               and row.get("reprt_code") == target.report
               and row.get("fs_div", target.basis) == target.basis
               and row.get("sj_div") == statement
               and row.get("account_id") == concept
               and row.get("rcept_no") == accession
               and (row.get("currency") or None) == unit]
    if len(matches) != 1:
        raise ValueError("raw account missing or ambiguous")
    value = matches[0].get("thstrm_amount")
    if value is None or isinstance(value, bool):
        raise ValueError("raw amount missing")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError("raw amount invalid") from exc
    if not number.is_finite():
        raise ValueError("raw amount not finite")
    return number


def _source(accession: str) -> str:
    if not re.fullmatch(r"[0-9]{14}", accession):
        raise ValueError("accession invalid")
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={accession}"


def audit_dart(path: Path, *, issuer: str, as_of: str) -> dict:
    """Compare every shown metric against its two archived API response rows."""
    base = {"version": VERSION, "issuer": issuer, "mode": "research_only",
            "not_order_advice": True, "source_available_at_verified": False}
    if not re.fullmatch(r"[0-9]{8}", issuer):
        return {**base, "status": "invalid_issuer", "checks": []}
    card = financial_change.describe_dart(path, ticker=issuer, issuer=issuer, as_of=as_of)
    if card["status"] == "archive_error":
        return {**base, "status": "audit_error", "reason": card.get("reason"), "checks": []}
    if card["status"] != "comparison":
        return {**base, "status": "not_ready", "card_status": card["status"],
                "reason": card.get("reason"), "checks": []}
    try:
        current = evidence.Target("dart", issuer, card["business_year"], card["report"], card["basis"])
        previous = evidence.Target("dart", issuer, card["prior_year"], card["report"], card["basis"])
        raws = (_raw(path, current, card["current_observation_id"], as_of=as_of),
                _raw(path, previous, card["prior_observation_id"], as_of=as_of))
        checks = []
        for key, metric in card["metrics"].items():
            concept, statement = _SPECS[key]
            unit = metric["unit"]
            current_accession, previous_accession = metric["current_accession"], metric["previous_accession"]
            now = _amount(raws[0], current, concept=concept, statement=statement,
                          accession=current_accession, unit=unit)
            before = _amount(raws[1], previous, concept=concept, statement=statement,
                             accession=previous_accession, unit=unit)
            expected_pct = round(float((now / before - 1) * 100), 1) if before > 0 else None
            matched = (now == Decimal(metric["current"]) and before == Decimal(metric["previous"])
                       and metric["change_pct"] == expected_pct
                       and metric["current_source"] == _source(current_accession)
                       and metric["previous_source"] == _source(previous_accession))
            checks.append({"metric": key, "status": "matched" if matched else "mismatch",
                           "concept": concept, "unit": unit, "current_year": current.year,
                           "previous_year": previous.year, "report": current.report,
                           "current_accession": current_accession,
                           "previous_accession": previous_accession,
                           "raw_current": str(now), "raw_previous": str(before),
                           "card_current": metric["current"], "card_previous": metric["previous"],
                           "card_change_pct": metric["change_pct"]})
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, InvalidOperation) as exc:
        return {**base, "status": "audit_error", "reason": type(exc).__name__, "checks": []}
    if not checks:
        return {**base, "status": "not_ready", "reason": "카드에 비교 숫자가 없습니다.", "checks": []}
    return {**base, "status": "matched" if all(item["status"] == "matched" for item in checks) else "mismatch",
            "card_status": card["status"], "current_observation_id": card["current_observation_id"],
            "previous_observation_id": card["prior_observation_id"], "checks": checks,
            "caveat": "보존된 API 응답과 화면 계산의 일치만 검사합니다. 원천 공시의 진위·발표시각·투자 성과는 검증하지 않습니다."}


def sample_dart(path: Path, *, as_of: str, count: int = 2) -> dict:
    """First comparable cards in issuer order, fixed before checking raw amounts.

    Missing prior reports cannot be audited as cards. Record every such
    exclusion, and fail closed on archive errors rather than selecting around
    them. A mismatch remains in the sample; it must never be skipped.
    """
    if not 1 <= count <= 5:
        raise ValueError("sample size must be 1..5")
    if not path.exists():
        return {"version": VERSION, "status": "not_recorded", "sample_size": count, "items": []}
    conn = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        urls = [row[0] for row in conn.execute(
            "SELECT DISTINCT source_url FROM financial_observations "
            "WHERE source_url LIKE 'https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?%' "
            "AND available_at<=?", (evidence._utc(as_of),)).fetchall()]
    except (OSError, sqlite3.Error, ValueError):
        return {"version": VERSION, "status": "archive_error", "sample_size": count, "items": []}
    finally:
        if conn is not None:
            conn.close()
    issuers = set()
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "opendart.fss.or.kr":
            continue
        code = urllib.parse.parse_qs(parsed.query).get("corp_code", [""])[0]
        if re.fullmatch(r"[0-9]{8}", code):
            issuers.add(code)
    selected = []
    excluded = []
    for issuer in sorted(issuers):
        card = financial_change.describe_dart(path, ticker=issuer, issuer=issuer, as_of=as_of)
        if card["status"] == "archive_error":
            excluded.append({"issuer": issuer, "card_status": "archive_error"})
            return {"version": VERSION, "status": "archive_error", "sample_size": count,
                    "observed": len(issuers), "selected": 0, "matched": 0,
                    "excluded": excluded, "items": []}
        if card["status"] == "comparison":
            selected.append(issuer)
            if len(selected) == count:
                break
        else:
            excluded.append({"issuer": issuer, "card_status": card["status"]})
    items = [audit_dart(path, issuer=issuer, as_of=as_of) for issuer in selected]
    matched = sum(item["status"] == "matched" for item in items)
    return {"version": VERSION,
            "status": "matched" if matched == count else "insufficient_sample" if len(selected) < count else "not_ready",
            "sample_size": count, "observed": len(issuers), "selected": len(selected),
            "matched": matched, "excluded": excluded, "items": items,
            "selection": f"관측 법인코드 순서대로 비교 카드가 성립하는 첫 {count}개를 금액 대조 전 선택; 제외 이유 공개",
            "note": "두 카드와 보존 원문이 맞아도 DART 사이트 원문·재배포 보존·사용자 이해도는 별도 검증이 필요합니다."}
