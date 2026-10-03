"""Read-only watchlist explanation from separately archived official statements.

Compares identical DART concepts, report types, statement scopes and units.
This is an observed accounting change, not an earnings surprise or buy signal.
"""

from __future__ import annotations

import sqlite3
import datetime as dt
from decimal import Decimal
from pathlib import Path

from signal_desk.ingest import financial_evidence as evidence

VERSION = "watchlist-financial-change-v1"
DEFAULT_ARCHIVE = Path("data/raw/financial-evidence.db")
_METRICS = {
    "revenue": ("ifrs-full_Revenue", "IS", "quarter", "thstrm_amount", "매출액"),
    "operating_income": ("dart_OperatingIncomeLoss", "IS", "quarter", "thstrm_amount", "영업이익"),
    "operating_cash_flow": ("ifrs-full_CashFlowsFromUsedInOperatingActivities", "CF",
                            "reported_duration", "thstrm_amount", "영업현금흐름"),
    "inventory": ("ifrs-full_Inventories", "BS", "instant", "thstrm_amount", "재고자산"),
}


def _choose(facts: list[dict], spec: tuple[str, str, str, str, str]) -> dict | None:
    concept, statement, period, amount_field, _ = spec
    rows = [row for row in facts if row.get("concept") == concept and row.get("statement") == statement
            and row.get("period_kind") == period and row.get("amount_field") == amount_field
            and row.get("basis") == "CFS" and row.get("unit")]
    # Multiple totals can arise from dimensions or corrections; never pick one by order.
    return rows[0] if len(rows) == 1 else None


def _percent(current: Decimal, previous: Decimal) -> float | None:
    if previous <= 0:
        return None
    return round(float((current / previous - 1) * 100), 1)


def _pair(current: list[dict], previous: list[dict], spec: tuple[str, str, str, str, str]) -> dict | None:
    left, right = _choose(current, spec), _choose(previous, spec)
    if not left or not right or left["unit"] != right["unit"]:
        return None
    now, before = Decimal(left["value"]), Decimal(right["value"])
    return {"label": spec[-1], "current": str(now), "previous": str(before),
            "unit": left["unit"], "change_pct": _percent(now, before),
            "current_source": left["filing_url"], "previous_source": right["filing_url"],
            "current_accession": left["accession"], "previous_accession": right["accession"]}


def _base(status: str, reason: str, *, ticker: str, issuer: str) -> dict:
    return {"version": VERSION, "status": status, "reason": reason, "ticker": ticker,
            "issuer": issuer, "market": "kr", "mode": "research_only", "not_order_advice": True,
            "source_available_at_verified": False}


def describe_dart(path: Path, *, ticker: str, issuer: str, as_of: str) -> dict:
    """Explain latest observed report against the same quarter of the prior year."""
    try:
        targets = evidence.dart_targets(path, issuer, as_of=as_of)
        observed = [(target, evidence.latest(path, target, as_of=as_of)) for target in targets]
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return _base("archive_error", "보존 기록을 검증할 수 없습니다.", ticker=ticker, issuer=issuer)
    eligible = [(target, item) for target, item in observed if item and item["status"] == "ok"]
    if not eligible:
        return _base("not_recorded", "아직 이 기업의 비교 가능한 공식 재무 관측이 없습니다.",
                     ticker=ticker, issuer=issuer)
    target, current = eligible[0]
    previous = next((item for candidate, item in eligible if
                     candidate.year == str(int(target.year) - 1) and candidate.report == target.report
                     and candidate.basis == target.basis), None)
    if previous is None:
        result = _base("need_prior_year", "같은 종류의 전년 보고서를 아직 수집하지 않았습니다.",
                       ticker=ticker, issuer=issuer)
        result.update({"business_year": target.year, "report": target.report,
                       "basis": target.basis, "current_observed_at": current["available_at"]})
        return result
    pairs = {name: pair for name, spec in _METRICS.items()
             if (pair := _pair(current["facts"], previous["facts"], spec)) is not None}
    if not pairs:
        return _base("no_comparable_facts", "계정·통화·기간·연결 기준이 일치하는 항목이 없습니다.",
                     ticker=ticker, issuer=issuer)

    good, caution = [], []
    rev = pairs.get("revenue")
    if rev and rev["change_pct"] is not None:
        direction = good if rev["change_pct"] > 0 else caution if rev["change_pct"] < 0 else None
        if direction is not None:
            direction.append(f"같은 분기 매출이 전년보다 {abs(rev['change_pct']):.1f}% "
                             + ("늘었습니다." if rev["change_pct"] > 0 else "줄었습니다."))

    margin_pp = None
    profit = pairs.get("operating_income")
    if rev and profit and rev["unit"] == profit["unit"]:
        current_rev, previous_rev = Decimal(rev["current"]), Decimal(rev["previous"])
        if current_rev > 0 and previous_rev > 0:
            margin_pp = round(float((Decimal(profit["current"]) / current_rev
                                     - Decimal(profit["previous"]) / previous_rev) * 100), 1)
            if margin_pp != 0:
                direction = good if margin_pp > 0 else caution
                direction.append(f"같은 분기 영업이익률이 {abs(margin_pp):.1f}%p "
                                 + ("높아졌습니다." if margin_pp > 0 else "낮아졌습니다."))

    cash = pairs.get("operating_cash_flow")
    if cash and Decimal(cash["current"]) < 0:
        caution.append("보고기간 영업현금흐름이 음수입니다.")
    inventory = pairs.get("inventory")
    if inventory and rev and inventory["change_pct"] is not None and rev["change_pct"] is not None:
        if inventory["change_pct"] > rev["change_pct"]:
            caution.append("재고 증가율이 매출 증가율보다 높습니다. 재고의 이유를 확인하세요.")

    checks = []
    if margin_pp is not None and margin_pp < 0:
        checks.append("다음 보고서에서도 영업이익률 하락이 이어지는지 확인하세요.")
    if cash is None or Decimal(cash["current"]) < 0:
        checks.append("영업현금흐름이 이익의 변화를 따라오는지 확인하세요.")
    if inventory and rev and inventory["change_pct"] is not None and rev["change_pct"] is not None:
        if inventory["change_pct"] > rev["change_pct"]:
            checks.append("다음 보고서에서 재고가 매출보다 빠르게 늘어나는지 확인하세요.")
    if not checks:
        checks.append("다음 분기에도 같은 계정과 회계기준으로 변화가 이어지는지 확인하세요.")
    return {**_base("comparison", "같은 종류의 전년 보고서와 비교했습니다.", ticker=ticker, issuer=issuer),
            "business_year": target.year, "prior_year": str(int(target.year) - 1),
            "report": target.report, "basis": target.basis,
            "current_observed_at": current["available_at"], "prior_observed_at": previous["available_at"],
            "current_observation_id": current["id"], "prior_observation_id": previous["id"],
            "metrics": pairs, "operating_margin_change_pp": margin_pp,
            "increases": good, "cautions": caution, "next_checks": checks,
            "caveat": "재무제표에 기록된 전년 대비 변화입니다. 발표 전 기대치·현재 주가의 반영 정도·미래 수익은 확인하지 않았습니다. 원천의 정확한 장중 공개시각은 미인증입니다."}


_SEC_REVENUE_TAGS = ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                     "us-gaap:Revenues", "us-gaap:SalesRevenueNet")
_SEC_METRICS = {"operating_income": ("us-gaap:OperatingIncomeLoss", "영업이익"),
                "net_income": ("us-gaap:NetIncomeLoss", "순이익"),
                "inventory": ("us-gaap:InventoryNet", "재고자산")}


def _sec_base(status: str, reason: str, *, ticker: str, issuer: str) -> dict:
    return {"version": VERSION, "status": status, "reason": reason, "ticker": ticker,
            "issuer": issuer, "market": "us", "mode": "research_only", "not_order_advice": True,
            "source_available_at_verified": False}


def _sec_version(rows: list[dict]) -> dict | None:
    """Latest filed amendment of one exact period; unresolved ties abstain."""
    if not rows or len({(r.get("period_start"), r.get("period_end"), r.get("unit")) for r in rows}) != 1:
        return None
    date = max(r["filing_date"] for r in rows)
    newest = [r for r in rows if r["filing_date"] == date]
    return newest[0] if len({(r["accession"], r["value"]) for r in newest}) == 1 else None


def _sec_pair(current: dict, previous: dict, label: str) -> dict:
    now, before = Decimal(current["value"]), Decimal(previous["value"])
    return {"label": label, "current": str(now), "previous": str(before),
            "unit": current["unit"], "change_pct": _percent(now, before),
            "current_source": current["filing_url"], "previous_source": previous["filing_url"],
            "current_accession": current["accession"], "previous_accession": previous["accession"],
            "current_period": [current.get("period_start"), current["period_end"]],
            "previous_period": [previous.get("period_start"), previous["period_end"]]}


def describe_sec(path: Path, *, ticker: str, issuer: str, as_of: str) -> dict:
    """Compare matched entity-wide SEC 10-Q facts, without pretending to replay PIT."""
    try:
        item = evidence.latest(path, evidence.Target("sec", issuer), as_of=as_of)
        cutoff = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00")).date().isoformat()
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return _sec_base("archive_error", "보존 기록을 검증할 수 없습니다.", ticker=ticker, issuer=issuer)
    if not item or item["status"] != "ok":
        return _sec_base("not_recorded", "아직 이 기업의 SEC 재무 관측이 없습니다.",
                         ticker=ticker, issuer=issuer)
    facts = [r for r in item["facts"] if r.get("form") in {"10-Q", "10-Q/A"}
             and r.get("unit") == "USD" and r.get("filing_date", "9999") <= cutoff
             and r.get("period_end", "9999") <= cutoff]
    revenue = [r for r in facts if r.get("concept") in _SEC_REVENUE_TAGS
               and r.get("period_kind") == "quarter_length"]
    if not revenue:
        return _sec_base("no_comparable_facts", "같은 분기 매출 항목을 찾지 못했습니다.",
                         ticker=ticker, issuer=issuer)
    latest_end = max(r["period_end"] for r in revenue)
    candidates = []
    for tag in _SEC_REVENUE_TAGS:
        current = _sec_version([r for r in revenue if r["concept"] == tag and r["period_end"] == latest_end])
        if not current:
            continue
        end = dt.date.fromisoformat(current["period_end"])
        start = dt.date.fromisoformat(current["period_start"])
        older = [r for r in revenue if r["concept"] == tag and r["period_end"] < latest_end
                 and r.get("duration_days") is not None
                 and abs(r["duration_days"] - current["duration_days"]) <= 7
                 and 350 <= (end - dt.date.fromisoformat(r["period_end"])).days <= 380
                 and 350 <= (start - dt.date.fromisoformat(r["period_start"])).days <= 380]
        if not older:
            continue
        distance = min(abs((end - dt.date.fromisoformat(r["period_end"])).days - 365) for r in older)
        closest = [r for r in older
                   if abs((end - dt.date.fromisoformat(r["period_end"])).days - 365) == distance]
        prior = _sec_version(closest)
        if prior:
            candidates.append((current, prior))
    if len(candidates) != 1:
        return _sec_base("no_comparable_facts", "같은 태그·단위·기간의 전년 매출이 한 쌍으로 확정되지 않았습니다.",
                         ticker=ticker, issuer=issuer)
    current_rev, prior_rev = candidates[0]
    current_end, prior_end = current_rev["period_end"], prior_rev["period_end"]
    pairs = {"revenue": _sec_pair(current_rev, prior_rev, "매출액")}
    for key, (tag, label) in _SEC_METRICS.items():
        kind = "instant" if key == "inventory" else "quarter_length"
        current = _sec_version([r for r in facts if r.get("concept") == tag and r.get("period_kind") == kind
                                and r["period_end"] == current_end])
        previous = _sec_version([r for r in facts if r.get("concept") == tag and r.get("period_kind") == kind
                                 and r["period_end"] == prior_end])
        if current and previous and current["unit"] == previous["unit"] == "USD":
            if key == "inventory" or (current["period_start"] == current_rev["period_start"]
                                      and previous["period_start"] == prior_rev["period_start"]):
                pairs[key] = _sec_pair(current, previous, label)
    increases, cautions = [], []
    change = pairs["revenue"]["change_pct"]
    if change is not None and change != 0:
        (increases if change > 0 else cautions).append(
            f"같은 분기 매출이 전년보다 {abs(change):.1f}% " + ("늘었습니다." if change > 0 else "줄었습니다."))
    next_checks = ["다음 10-Q에서도 같은 회계 태그·통화·분기 길이로 변화가 이어지는지 확인하세요."]
    return {**_sec_base("comparison", "같은 회계 태그와 기간의 전년 분기를 비교했습니다.",
                        ticker=ticker, issuer=issuer),
            "report": "10-Q", "business_year": current_end[:4], "prior_year": prior_end[:4],
            "current_observed_at": item["available_at"], "prior_observed_at": item["available_at"],
            "current_observation_id": item["id"], "prior_observation_id": item["id"],
            "metrics": pairs, "increases": increases, "cautions": cautions,
            "next_checks": next_checks,
            "caveat": "오늘 관측한 SEC 회사별 사실의 과거 보고기간 비교입니다. 당시 장중 공개시각·예상 대비 실적·주가 반영·미래 수익을 검증한 것이 아닙니다."}
