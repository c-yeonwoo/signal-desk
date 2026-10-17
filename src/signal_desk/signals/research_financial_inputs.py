"""Receipt-dated annual financial candidates, never an as-run/PIT certificate."""

from __future__ import annotations

import datetime as dt
import re
import io
import xml.etree.ElementTree as ET
from decimal import Decimal
from zipfile import ZipFile

from signal_desk import market_clock
from signal_desk.ingest import dart
from signal_desk.signals import pit_fundamentals, quality


def normalize_annual(rows: list[dict], *, ticker: str, corp_code: str, year: int) -> dict:
    """Keep a single filing, currency and statement basis; never mix CFS/OFS."""
    if not rows:
        return {"ticker": ticker, "year": year, "status": "missing_accounts"}
    if any(r.get("stock_code") != ticker or r.get("corp_code") != corp_code
           or r.get("bsns_year") != str(year) or r.get("reprt_code") != "11011"
           for r in rows):
        raise ValueError("financial company/year identity mismatch")
    basis = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
    selected = [r for r in rows if r.get("fs_div") == basis]
    receipts = {r.get("rcept_no") for r in selected}
    if len(receipts) != 1 or not selected or any(r.get("currency") != "KRW" for r in selected):
        raise ValueError("financial filing or currency is ambiguous")
    receipt = next(iter(receipts))
    if not isinstance(receipt, str) or not re.fullmatch(r"\d{14}", receipt):
        raise ValueError("invalid financial receipt")
    filed = dt.date.fromisoformat(f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}")
    if filed.year <= year:
        raise ValueError("annual filing precedes fiscal year end")
    # The receipt has a date, not an intraday publication timestamp.
    after = filed + dt.timedelta(days=1)
    while not market_clock.is_session("kr", after):
        after += dt.timedelta(days=1)
    core = {}
    for prefix in ("자본총계", "부채총계", "당기순이익", "매출액"):
        matches = [r for r in selected if str(r.get("account_nm", "")).startswith(prefix)]
        # DART can repeat an identical income line with a different ord value.
        # Only identical facts are duplicates; differing amounts stay ambiguous.
        facts = {tuple(r.get(k) for k in ("account_nm", "sj_div", "thstrm_dt", "thstrm_amount",
                                          "frmtrm_dt", "frmtrm_amount")) for r in matches}
        if len(facts) > 1:
            raise ValueError("ambiguous financial account")
        if matches:
            row = matches[0]
            if not re.search(rf"\b{year}[.]12[.]31\b", row.get("thstrm_dt", "")):
                return {"ticker": ticker, "year": year, "status": "non_december_or_unverified_period"}
            core[prefix] = row
    metrics = dart._derive_metrics(list(core.values()))
    return {"ticker": ticker, "corp_code": corp_code, "year": year, "status": "receipt_dated_candidate",
            "receipt": receipt, "filing_date": filed.isoformat(), "fs_div": basis,
            "usable_from_session_assumption": after.isoformat(), "currency": "KRW",
            "metrics": metrics, "strict_pit_eligible": False,
            "original_document_matched": False}


def original_annual(raw: bytes, *, ticker: str, corp_code: str, year: int, receipt: str) -> dict:
    """Narrow receipt-specific XBRL totals; no segmented/custom-account guesses."""
    ns = {"x": "http://www.xbrl.org/2003/instance", "d": "http://xbrl.org/2006/xbrldi"}
    with ZipFile(io.BytesIO(raw)) as archive:
        names = [i for i in archive.infolist() if i.filename.endswith(".xbrl")]
        if len(names) != 1 or names[0].file_size > 32_000_000:
            raise ValueError("original XBRL scope differs")
        root = ET.fromstring(archive.read(names[0]))
    contexts = {e.get("id"): e for e in root.findall("x:context", ns)}
    units = {u.get("id"): u.findtext("x:measure", namespaces=ns) for u in root.findall("x:unit", ns)}
    # Never fall back to OFS merely because a CFS concept is missing.
    consolidated = any(e.text == "ifrs-full:ConsolidatedMember"
                       for c in contexts.values() for e in c.findall(".//d:explicitMember", ns))
    basis = "CFS" if consolidated else "OFS"
    member = "ConsolidatedMember" if consolidated else "SeparateMember"
    values = {}
    for e in root:
        concept = e.tag.rsplit("}", 1)[-1]
        if concept not in ("Equity", "Liabilities", "ProfitLoss", "Revenue") or "ifrs-full}" not in e.tag:
            continue
        c = contexts.get(e.get("contextRef"))
        if c is None:
            continue
        dims = [(d.get("dimension"), d.text) for d in c.findall(".//d:explicitMember", ns)]
        if (dims != [("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", f"ifrs-full:{member}")]
                or c.findall(".//d:typedMember", ns)
                or c.findtext("x:entity/x:identifier", namespaces=ns) != corp_code
                or units.get(e.get("unitRef")) != "iso4217:KRW"):
            continue
        instant = c.findtext("x:period/x:instant", namespaces=ns)
        end = instant or c.findtext("x:period/x:endDate", namespaces=ns)
        start = c.findtext("x:period/x:startDate", namespaces=ns)
        if (not end or end[:4] not in (str(year), str(year - 1)) or end[4:] != "-12-31"
                or (concept in ("Equity", "Liabilities") and not instant)
                or (concept in ("ProfitLoss", "Revenue") and (instant or start != end[:4] + "-01-01"))):
            continue
        if e.text is None:  # nil is unavailable, not zero.
            continue
        value = Decimal(e.text)
        key = concept, int(end[:4])
        if not value.is_finite() or key in values and values[key] != value:
            raise ValueError("conflicting original XBRL totals")
        values[key] = value
    required = [(k, year) for k in ("Equity", "Liabilities", "ProfitLoss")]
    if any(k not in values for k in required):
        raise ValueError("standard whole-statement KRW totals unavailable")
    rows = [{"stock_code": ticker, "corp_code": corp_code, "bsns_year": str(year),
             "reprt_code": "11011", "rcept_no": receipt, "currency": "KRW", "fs_div": basis,
             "account_nm": name, "thstrm_dt": f"{year}.12.31", "thstrm_amount": str(values[k, year]),
             "frmtrm_amount": str(values.get((k, year - 1), ""))}
            for k, name in (("Equity", "자본총계"), ("Liabilities", "부채총계"),
                            ("ProfitLoss", "당기순이익"), ("Revenue", "매출액")) if (k, year) in values]
    return {**normalize_annual(rows, ticker=ticker, corp_code=corp_code, year=year),
            "original_document_matched": True}


def financial_versions_asof(base: dict, day: str, versions: dict) -> dict:
    """Use the last dated version, including failures; do not carry through a correction."""
    if not versions["start"] <= day <= versions["end"]:
        return base
    result = dict(base)
    for ticker in versions["tickers"]:
        available = [r for r in versions["records"] if r["ticker"] == ticker
                     and r["usable_from_session_assumption"] <= day]
        if not available:
            result[ticker, 2025] = {"status": "original_not_available_at_decision"}
            continue
        latest = max(r["filing_date"] for r in available)
        same_day = [r for r in available if r["filing_date"] == latest]
        facts = {(r["status"], r.get("fs_div"), tuple(sorted(r.get("metrics", {}).items()))) for r in same_day}
        result[ticker, 2025] = (same_day[0] if len(facts) == 1 else {"status": "same_day_version_order_unverified"})
    return result


def metrics_asof(records: list[dict], day: str, caps: dict[str, float], *, versions: dict | None = None) -> tuple[dict, dict]:
    """Research sensitivity: April FY convention plus an actual receipt-date guard."""
    if not market_clock.is_session("kr", day) or day >= "2026-08-05":
        raise ValueError("financial replay is outside domestic development sessions")
    year = pit_fundamentals.latest_fiscal_year(day)
    by_key = {(r["ticker"], r["year"]): r for r in records}
    if len(by_key) != len(records):
        raise ValueError("duplicate financial ticker-year")
    if versions is not None:
        by_key = financial_versions_asof(by_key, day, versions)
    out, excluded = {}, {}
    for ticker, cap in caps.items():
        current = by_key.get((ticker, year), {})
        if current.get("status") != "receipt_dated_candidate":
            excluded[ticker] = current.get("status", "missing_fiscal_year")
            continue
        if current["usable_from_session_assumption"] > day:
            excluded[ticker] = "returned_filing_not_available_at_decision"
            continue
        if not cap or cap <= 0:
            excluded[ticker] = "missing_historical_market_cap"
            continue
        m = dict(current["metrics"])
        previous = by_key.get((ticker, year - 1), {})
        prev = (previous.get("metrics", {}) if previous.get("status") == "receipt_dated_candidate"
                and previous["usable_from_session_assumption"] <= day
                and previous["fs_div"] == current["fs_div"] else {})
        m["quality"] = quality.evaluate(m, prev)
        ni, equity = m.get("net_income"), m.get("equity")
        if ni is not None and ni > 0:
            m["per"] = round(cap / ni, 2)
        if equity is not None and equity > 0:
            m["pbr"] = round(cap / equity, 2)
        out[ticker] = m
    return out, excluded
