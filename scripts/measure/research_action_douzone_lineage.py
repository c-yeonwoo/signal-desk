"""Research-only Douzone delisting price lineage from frozen KRX/KIND bytes.

The exchange consideration is a disclosed schedule, not proof that an old
holder received cash. This module never changes live prices or trading rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import SourceError, verify_action_archive
from scripts.measure.krx_price_replay_pilot import ROOT, load_krx_archives
from scripts.measure.research_action_price_lineage import Case, analyze_price_lineage

KRX_ARCHIVE = "krx-daily-2026-06-07-v1.zip"
KRX_SHA256 = "ce595d2bf8b7b43dbe3266fdb19615987b8945e517ccaba500db46c76a31c15d"
KIND_ARCHIVE = "kind-douzone-review-2026-10-12-v1.zip"
KIND_SHA256 = "32038c08febec524258a0b829aec1967131a4d4ddfb25bf2809ae211ea66cdfc"
KIND_MEMBERS = {
    "raw/douzone-exchange-terms-20260519.htm": (
        "963859169b833f594b934b8a855be7f99b8259c64bebd3ccfe11b5bf1144c440",
        ("더존비즈온", "도로니쿰", "120,000", "2026년 6월 26일", "2026년 7월 15일")),
    "raw/douzone-suspension-20260619.htm": (
        "865330de39ea4a1b14b756cdd0d0b1da79ec9c391ae0a2f319ad82e3ab8bf05e",
        ("더존비즈온", "도로니쿰", "2026년06월26일", "주식교환")),
}
# analyze_price_lineage uses only the ticker, successor, disappearance date and
# price rows. KIND evidence is verified separately; never pass it as DART.
CASE = Case("012510", None, "2026-07-15", ((KRX_ARCHIVE, KRX_SHA256),),
            ("", ""), "", "", "")


def _verify_kind(path: Path) -> list[dict]:
    checked = verify_action_archive(path)
    if checked["source"] != "KRX KIND originals" or checked["sha256"] != KIND_SHA256:
        raise SourceError("Douzone lineage: frozen KIND archive differs")
    results = []
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if set(manifest["entries"]) != set(KIND_MEMBERS):
            raise SourceError("Douzone lineage: KIND document set differs")
        for name, (expected_sha, phrases) in KIND_MEMBERS.items():
            entry = manifest["entries"][name]
            if entry["sha256"] != expected_sha:
                raise SourceError("Douzone lineage: KIND original hash differs")
            flattened = re.sub(r"<[^>]*>|\s+", " ", archive.read(name).decode("utf-8"))
            if any(phrase not in flattened for phrase in phrases):
                raise SourceError("Douzone lineage: KIND event text differs")
            results.append({"document_id": entry["document_id"],
                            "source_filing_date": entry["source_filing_date"],
                            "url": entry["url"], "raw_sha256": expected_sha})
    return results


def review(data_dir: Path) -> dict:
    price_path = data_dir / KRX_ARCHIVE
    if hashlib.sha256(price_path.read_bytes()).hexdigest() != KRX_SHA256:
        raise ValueError("Douzone lineage: frozen KRX archive differs")
    kind_documents = _verify_kind(data_dir / KIND_ARCHIVE)
    days, price_sources = load_krx_archives([price_path])
    lineage = analyze_price_lineage(days, CASE)
    if (lineage["last_traded"]["session"] != "2026-06-25"
            or lineage["zero_volume_carry"] != {
                "start": "2026-06-26", "end": "2026-07-14", "sessions": 13,
                "carried_close_krw": 120000,
            }):
        raise ValueError("Douzone lineage: unexpected trade/suspension interval")
    return {"schema": "research-action-douzone-lineage-v1", "research_only": True,
            "case": lineage, "price_sources": price_sources,
            "kind_archive_sha256": KIND_SHA256, "kind_documents": kind_documents,
            "scheduled_cash_per_common_share_krw": 120000,
            "actual_cash_payment_confirmed": False,
            "holder_total_return_verified": False,
            "registered_verdict": False,
            "limitations": ["The 120,000 KRW price is a disclosed schedule, not a receipt",
                            "A zero-volume carried close is not executable",
                            "Selected KIND filings do not certify the complete correction chain",
                            "The listed security disappears; it must not be silently removed from the comparator"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    args = parser.parse_args()
    print(json.dumps(review(args.data_dir), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
