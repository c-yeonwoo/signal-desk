"""Review one cash exchange's scheduled terms and later effect separately.

Two selected KIND originals are not a complete event feed. This audit does not
assert a cash payment, a tax result, a holder's eligibility, or a total return.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import verify_action_archive
from scripts.measure.krx_price_replay_pilot import ROOT

ARCHIVE = ROOT / "data/research/kind-ssangyong-cash-exchange-2026-10-12-v1.zip"
ARCHIVE_SHA256 = "b73b2c2c6bf91155e841891aff924a01aab004cac4da4502ec494d1ffc6d39a9"
PLAN = ROOT / "docs/research-ssangyong-cash-exchange-kind-plan-2026-10-12.json"
PLAN_SHA256 = "89551a68f846c1ceb0227857767e4dc50999c5e00f0b0edc3dba62fc530c8c23"
TERMS_ID = "ssangyong-exchange-terms-20240423"
EFFECT_ID = "ssangyong-exchange-effect-20240702"


def _text(raw: bytes) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ",
                                                   raw.decode("utf-8")))).strip()


def _one_date(pattern: str, text: str, *, label: str) -> str:
    found = re.findall(pattern, text)
    if len(found) != 1:
        raise ValueError(f"cash exchange: {label} is ambiguous or absent")
    try:
        return dt.date(*(int(value) for value in found[0])).isoformat()
    except ValueError as exc:
        raise ValueError(f"cash exchange: {label} is invalid") from exc


def parse_terms(raw: bytes) -> dict:
    text = _text(raw)
    if ("쌍용씨앤이" not in text or "한앤코시멘트홀딩스" not in text
            or "현금교부형 주식교환" not in text
            or "보통주식" not in text or "교부할 예정" not in text):
        raise ValueError("cash exchange: issuer, class, payer or planned terms differ")
    amounts = {int(value.replace(",", "")) for value in
               re.findall(r"주당\s*현금\s*([\d,]+)원", text)}
    if amounts != {7000}:
        raise ValueError("cash exchange: scheduled common-share amount differs")
    effective = _one_date(
        r"교환ㆍ이전일자\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일",
        text, label="planned exchange date")
    return {"ticker": "003410", "share_class": "ordinary_common",
            "payer": "한앤코시멘트홀딩스(주)", "scheduled_gross_krw_per_share": 7000,
            "planned_effective_on": effective, "amount_status": "scheduled",
            "payment_date": None, "payment_status": "not_confirmed_by_selected_originals"}


def parse_effect(raw: bytes) -> dict:
    text = _text(raw)
    if ("쌍용씨앤이주식회사" not in text or "보통주" not in text
            or "한앤코시멘트홀딩스" not in text
            or "완전자회사로 편입됨에 따라 상장폐지함" not in text):
        raise ValueError("cash exchange: completed listing identity differs")
    exchange = _one_date(
        r"주식교환\s*\(교환일:\s*(\d{4})\.(\d{2})\.(\d{2})\)",
        text, label="completed exchange date")
    delisting = re.findall(r"6\.상장폐지일\s*(\d{4}-\d{2}-\d{2})", text)
    if len(delisting) != 1 or dt.date.fromisoformat(delisting[0]) <= dt.date.fromisoformat(exchange):
        raise ValueError("cash exchange: delisting date differs")
    return {"effective_on": exchange, "delisting_on": delisting[0],
            "effect_status": "confirmed_by_later_KRX_delisting_notice",
            "cash_payment_confirmed": False}


def audit(archive_path: Path = ARCHIVE, plan_path: Path = PLAN) -> dict:
    if (hashlib.sha256(plan_path.read_bytes()).hexdigest() != PLAN_SHA256
            or hashlib.sha256(archive_path.read_bytes()).hexdigest() != ARCHIVE_SHA256):
        raise ValueError("cash exchange: frozen plan/archive SHA differs")
    checked = verify_action_archive(archive_path)
    if (checked["source"] != "KRX KIND originals"
            or checked["source_state"] != "selected_originals_not_action_complete"
            or checked["raw_members"] != 2):
        raise ValueError("cash exchange: source archive scope differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    planned = {item["id"]: item for item in plan["documents"]}
    if (plan["schema"] != "research-kind-review-plan-v1"
            or set(planned) != {TERMS_ID, EFFECT_ID}):
        raise ValueError("cash exchange: selected source plan differs")
    originals = {}
    with ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for document_id in (TERMS_ID, EFFECT_ID):
            name = f"raw/{document_id}.htm"
            entry = manifest["entries"].get(name)
            if (entry is None or entry["url"] != planned[document_id]["url"]
                    or entry["document_id"] != document_id
                    or entry["expected_text"] != "쌍용씨앤이"):
                raise ValueError("cash exchange: source URL or issuer marker differs")
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("cash exchange: saved original hash differs")
            originals[document_id] = {"raw": raw, "entry": entry}
    terms = parse_terms(originals[TERMS_ID]["raw"])
    effect = parse_effect(originals[EFFECT_ID]["raw"])
    if terms["planned_effective_on"] != effect["effective_on"]:
        raise ValueError("cash exchange: planned and observed effective dates conflict")
    return {"schema": "research-cash-exchange-source-audit-v1",
            "research_only": True, "ticker": terms["ticker"],
            "plan_sha256": PLAN_SHA256, "archive_sha256": ARCHIVE_SHA256,
            "sources": {document_id: {
                "url": originals[document_id]["entry"]["url"],
                "raw_sha256": originals[document_id]["entry"]["sha256"],
                "filing_date": originals[document_id]["entry"]["source_filing_date"],
                "observed_at_utc": originals[document_id]["entry"]["retrieved_at_utc"]}
                for document_id in (TERMS_ID, EFFECT_ID)},
            "terms": terms, "effect": effect,
            "holder_entitlement_verified": False,
            "payment_completion_verified": False,
            "feed_eligible": False, "action_coverage_state": "partial",
            "economic_return_pct": None, "simulated_liquidated_return_pct": None,
            "strict_pit_eligible": False, "registered_verdict": False,
            "live_order_eligible": False,
            "missing": ["final cash consideration/correction chain",
                        "payment date and actual payment confirmation",
                        "whole-period action coverage and historical holder eligibility"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2,
                  allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output),
                      "effect_status": report["effect"]["effect_status"],
                      "payment_completion_verified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
