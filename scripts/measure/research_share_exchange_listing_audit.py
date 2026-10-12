"""Cross-check two selected post-merger KIND originals with frozen source audit.

One annual filing confirms shares issued in aggregate; one KRX notice identifies
a merger addition to an existing listing. Neither proves individual delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import verify_action_archive
from scripts.measure.krx_price_replay_pilot import ROOT


ARCHIVE = ROOT / "data/research/kind-share-exchange-review-2026-10-12-v1.zip"
ARCHIVE_SHA256 = "fd495120a826e94a5b63472f64ef5381212b598502011c187dbed6d29da8c859"
PLAN = ROOT / "docs/research-share-exchange-kind-plan-2026-10-12.json"
PLAN_SHA256 = "68710d356a0e95cf36d37f184c5938eb6799d5fd5a0e04eb5af0e140e27dc3a3"
TERMS_REPORT = ROOT / "docs/research-share-exchange-source-audit-2026-10-12.json"
TERMS_REPORT_SHA256 = "4cad8e9b37c9cff3b443e2edb2b7f0654a81627fec8b390325b6fd2617e3fd5f"
HEAVY_ID = "hhi-issued-shares-annual-20260320"
CONSTRUCTION_ID = "hce-additional-listing-20260121"


def _text(raw: bytes) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", raw.decode("utf-8"))))


def parse_heavy_annual(raw: bytes) -> dict:
    text = _text(raw)
    if ("HD현대중공업" not in text or "HD현대미포" not in text
            or "2025년 12월 1일 흡수합병" not in text):
        raise ValueError("heavy annual: issuer or effected merger absent")
    issued = {int(value.replace(",", "")) for value in re.findall(
        r"신주 발행 \(유통주식수\)\s*([\d,]+)", text)}
    if 16188109 not in issued:
        raise ValueError("heavy annual: merger issuance count absent")
    return {"ticker": "329180", "merger_issued_common_shares": 16188109,
            "aggregate_issuance_retrospectively_confirmed": True,
            "new_share_listing_session_confirmed": False,
            "individual_share_delivery_confirmed": False}


def parse_construction_notice(raw: bytes) -> dict:
    text = _text(raw)
    if ("에이치디현대건설기계(주) 보통주 추가상장" not in text
            or "기명식 보통주 30,616,505주" not in text
            or "증자방법 : 합병" not in text
            or "상장일 : 2026년01월26일" not in text
            or "발행일 : 2026년01월02일" not in text
            or "KR7267270007 (단축코드:A267270)" not in text):
        raise ValueError("construction listing: merger shares, date or security identity differ")
    return {"ticker": "267270", "isin": "KR7267270007",
            "merger_issued_common_shares": 30616505,
            "exchange_notice_published_on": "2026-01-21",
            "notice_listing_on": "2026-01-26",
            "new_share_listing_notice_confirmed": True,
            "individual_share_delivery_confirmed": False}


def audit(archive_path: Path = ARCHIVE, plan_path: Path = PLAN,
          terms_report_path: Path = TERMS_REPORT) -> dict:
    if (hashlib.sha256(archive_path.read_bytes()).hexdigest() != ARCHIVE_SHA256
            or hashlib.sha256(plan_path.read_bytes()).hexdigest() != PLAN_SHA256):
        raise ValueError("share listing: frozen archive or plan SHA differs")
    checked = verify_action_archive(archive_path)
    if (checked["source"] != "KRX KIND originals"
            or checked["source_state"] != "selected_originals_not_action_complete"
            or checked["raw_members"] != 2):
        raise ValueError("share listing: source scope differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    planned = {item["id"]: item for item in plan["documents"]}
    if plan["schema"] != "research-kind-review-plan-v1" or set(planned) != {HEAVY_ID, CONSTRUCTION_ID}:
        raise ValueError("share listing: selected source plan differs")
    if hashlib.sha256(terms_report_path.read_bytes()).hexdigest() != TERMS_REPORT_SHA256:
        raise ValueError("share listing: underlying terms report SHA differs")
    terms_report = json.loads(terms_report_path.read_text(encoding="utf-8"))
    if terms_report["schema"] != "research-share-exchange-source-audit-v1":
        raise ValueError("share listing: underlying terms audit differs")
    indexed = {row["terms"]["old_ticker"]: row for row in terms_report["cases"]}
    if set(indexed) != {"010620", "042670"}:
        raise ValueError("share listing: underlying case set differs")
    sources = {}
    raw = {}
    with ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for document_id in (HEAVY_ID, CONSTRUCTION_ID):
            name = f"raw/{document_id}.htm"
            entry = manifest["entries"].get(name)
            if (entry is None or entry["url"] != planned[document_id]["url"]
                    or entry["document_id"] != document_id
                    or entry["expected_text"] != planned[document_id]["expected_text"]):
                raise ValueError("share listing: saved source identity differs")
            raw[document_id] = archive.read(name)
            if hashlib.sha256(raw[document_id]).hexdigest() != entry["sha256"]:
                raise ValueError("share listing: saved original hash differs")
            sources[document_id] = {"url": entry["url"], "raw_sha256": entry["sha256"],
                                    "filing_date": entry["source_filing_date"],
                                    "observed_at_utc": entry["retrieved_at_utc"]}
    heavy = parse_heavy_annual(raw[HEAVY_ID])
    construction = parse_construction_notice(raw[CONSTRUCTION_ID])
    if (heavy["merger_issued_common_shares"] != indexed["010620"]["terms"]["company_issued_common_shares"]
            or construction["merger_issued_common_shares"] != indexed["042670"]["terms"]["company_issued_common_shares"]
            or construction["notice_listing_on"] != indexed["042670"]["first_observed_successor_trade"]["session"]):
        raise ValueError("share listing: later source and pinned terms/price disagree")
    return {"schema": "research-share-exchange-listing-audit-v1",
            "research_only": True,
            "kind_plan_sha256": PLAN_SHA256,
            "kind_archive_sha256": ARCHIVE_SHA256,
            "underlying_terms_report_sha256": TERMS_REPORT_SHA256,
            "sources": sources,
            "cases": {"010620": heavy, "042670": construction},
            "holder_entitlement_verified": False,
            "fractional_cash_payment_verified": False,
            "economic_return_pct": None,
            "action_coverage_state": "partial", "feed_eligible": False,
            "strict_pit_eligible": False, "registered_verdict": False,
            "live_order_eligible": False,
            "missing": ["HD현대중공업 merger-share batch's KRX listing notice/date",
                        "actual holder share delivery, ratio precision/rounding and fractional cash payment",
                        "complete correction and corporate-action coverage"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2,
                  allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "source_count": len(report["sources"]),
                      "economic_return_pct": None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
