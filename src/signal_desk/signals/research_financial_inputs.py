"""Receipt-dated annual financial candidates, never an as-run/PIT certificate."""

from __future__ import annotations

import datetime as dt
import re

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


def metrics_asof(records: list[dict], day: str, caps: dict[str, float]) -> tuple[dict, dict]:
    """Research sensitivity: April FY convention plus an actual receipt-date guard."""
    if not market_clock.is_session("kr", day) or day >= "2026-08-05":
        raise ValueError("financial replay is outside domestic development sessions")
    year = pit_fundamentals.latest_fiscal_year(day)
    by_key = {(r["ticker"], r["year"]): r for r in records}
    if len(by_key) != len(records):
        raise ValueError("duplicate financial ticker-year")
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
