"""Audit two frozen merger filings against KRX price lineage, research only.

The issued-share notice, individual delivery, fractional-cash rounding and
payment are not proven by a merger report or a successor's traded close.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.krx_price_replay_pilot import ROOT
from scripts.measure.research_action_price_lineage import CASES, review


@dataclass(frozen=True)
class Terms:
    old_ticker: str
    old_issuer: str
    successor_issuer: str
    record_date: str
    effective_on: str
    planned_listing_on: str
    ratio: str
    issued_common_shares: int
    ratio_precision: str


TERMS = (
    Terms("010620", "HD현대미포", "HD현대중공업", "2025-11-28",
          "2025-12-01", "2025-12-15", "0.4059146", 16188109,
          "filing_displayed_7_decimal_places"),
    Terms("042670", "에이치디현대인프라코어", "에이치디현대건설기계",
          "2025-12-31", "2026-01-01", "2026-01-26", "0.1621707",
          30616505, "filing_explicitly_says_displayed_7_decimal_places_only"),
)


def _flatten(raw: bytes) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", raw.decode("utf-8"))).strip()


def _date_tokens(date: str) -> tuple[str, str, str]:
    year, month, day = date.split("-")
    return year, str(int(month)), str(int(day))


def parse_terms(raw: bytes, terms: Terms) -> dict:
    text = _flatten(raw)
    if terms.old_issuer not in text or terms.successor_issuer not in text:
        raise ValueError("share exchange: filing issuer identity differs")
    if terms.ratio not in text or text.count(terms.ratio) != 1:
        raise ValueError("share exchange: final reported ratio absent or ambiguous")
    if not re.search(r"(교부\s*비율|교부비율).{0,100}" + re.escape(terms.ratio), text):
        raise ValueError("share exchange: ratio is not the holder exchange term")
    if ("기명식 보통주" not in text and "보통주" not in text) or "단주" not in text:
        raise ValueError("share exchange: share class or fractional right absent")
    if "주권상장일에 한국거래소에서 거래되는 종가" not in text:
        raise ValueError("share exchange: fractional cash price rule absent")
    if "현금으로 지급할 예정" not in text:
        raise ValueError("share exchange: fractional cash is not an identified plan")
    for label, date in (("record date", terms.record_date),
                        ("effect date", terms.effective_on),
                        ("planned listing", terms.planned_listing_on)):
        year, month, day = _date_tokens(date)
        pattern = rf"{year}년\s*0?{month}월\s*0?{day}일"
        if not re.search(pattern, text):
            raise ValueError(f"share exchange: {label} differs")
    issued = re.findall(r"발행\s*신주의\s*수\s*:\s*([\d,]+)주", text)
    if len(issued) != 1 or int(issued[0].replace(",", "")) != terms.issued_common_shares:
        raise ValueError("share exchange: issued common share count differs")
    if (terms.old_ticker == "042670"
            and "소수점 이하 일곱째 자리까지만 표시함" not in text):
        raise ValueError("share exchange: ratio display precision note absent")
    return {"old_ticker": terms.old_ticker,
            "old_issuer": terms.old_issuer,
            "successor_issuer": terms.successor_issuer,
            "share_class": "ordinary_common",
            "holder_record_date": terms.record_date,
            "merger_effective_on": terms.effective_on,
            "planned_new_listing_on": terms.planned_listing_on,
            "reported_shares_per_old_share": terms.ratio,
            "reported_ratio_precision": terms.ratio_precision,
            "company_issued_common_shares": terms.issued_common_shares,
            "fractional_cash_price_rule": "new_listing_session_KRX_close",
            "fractional_cash_status": "planned_not_confirmed_paid"}


def audit(data_dir: Path = ROOT / "data/research") -> dict:
    lineage = review(data_dir)
    indexed = {item["old_ticker"]: item for item in lineage["cases"]}
    rows = []
    for terms in TERMS:
        case = next(c for c in CASES if c.old_ticker == terms.old_ticker)
        observed = indexed[terms.old_ticker]
        if (case.successor_ticker != observed["successor_ticker"]
                or case.consideration_text != terms.ratio):
            raise ValueError("share exchange: pinned case identity differs")
        archive_name, archive_sha = case.dart_archive
        with ZipFile(data_dir / archive_name) as archive:
            with ZipFile(io.BytesIO(archive.read(f"raw/doc/{case.receipt}.zip"))) as filing:
                parsed = parse_terms(filing.read(f"{case.receipt}.xml"), terms)
        first = observed["successor_first_five_trading_sessions"][0]
        if first["session"] != terms.planned_listing_on:
            raise ValueError("share exchange: first observed successor session differs")
        rows.append({"terms": parsed,
                     "dart_receipt": case.receipt,
                     "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={case.receipt}",
                     "dart_archive_sha256": archive_sha,
                     "dart_original_sha256": observed["dart_original_sha256"],
                     "krx_price_archives_sha256": {
                         name: sha for name, sha in case.price_archives},
                     "successor_ticker": case.successor_ticker,
                     "first_observed_successor_trade": first,
                     "old_last_traded": observed["last_traded"],
                     "old_zero_volume_carry": observed["zero_volume_carry"],
                     "issuer_effect_source": "post_merger_report",
                     "listing_completion_verified": False,
                     "holder_record_date_eligibility_verified": False,
                     "account_share_delivery_verified": False,
                     "fractional_cash_payment_verified": False,
                     "fractional_cash_rounded_amount_krw": None,
                     "economic_return_pct": None,
                     "action_coverage_state": "partial",
                     "feed_eligible": False})
    return {"schema": "research-share-exchange-source-audit-v1",
            "research_only": True, "strict_pit_eligible": False,
            "registered_verdict": False, "live_order_eligible": False,
            "cases": rows,
            "limitations": [
                "The later merger report confirms terms and effect but lists the new listing as planned",
                "A successor close is not proof of actual issuance or individual account delivery",
                "A completed appraisal-right buyback payment is not fractional-share cash payment",
                "Displayed exchange ratios and unrounded formula values are not actual rounded credits",
                "Selected filings are not a complete correction or corporate-action feed"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.data_dir)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, sort_keys=True, indent=2,
                  allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "cases": len(result["cases"]),
                      "verified_returns": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
