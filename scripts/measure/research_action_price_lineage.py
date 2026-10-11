"""Research-only corporate-action price lineage from frozen official responses.

The surviving ticker's price is not a realized return for the old holder:
account delivery, fractional cash, corrections and payment are still unresolved.
No live cache, registered verdict, or order path is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from scripts.measure.acquire_historical_sources import SourceError, verify_action_archive
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives


@dataclass(frozen=True)
class Case:
    old_ticker: str
    successor_ticker: str | None
    first_absent: str
    price_archives: tuple[tuple[str, str], ...]
    dart_archive: tuple[str, str]
    receipt: str
    old_issuer_text: str
    consideration_text: str


CASES = (
    Case("003410", None, "2024-07-09", (
        ("krx-daily-2024-q2-v1.zip", "ee7d94c0524165a07c4907e5ccf967632d2071e26329c29938b69f2f9173b4df"),
        ("krx-daily-2024-q3-v1.zip", "894466cf70e9ef64bcc7caedeeb3d24c498755f3d4cd13ecc86df9432e7cbf9e")),
        ("dart-actions-00138224-2024-06-20-30-v1.zip", "cbc31dc6d094037bbc38926bbdb4daaf93db65429bee7f5d776a610b0571c6f7"),
        "20240625000452", "쌍용씨앤이", "7,000"),
    Case("010620", "329180", "2025-12-15", (
        ("krx-daily-2025-09-12-v1.zip", "e21a3df2065aa43853b827a3f72281d59bcad5d22b28d7a6066658bbd6a77f01"),),
        ("dart-actions-01390344-2025-11-20-12-20-v1.zip", "cd0ce52bc5543a11fae58968423eab6cb9520eab149de80d1947ae76340f6376"),
        "20251201000400", "HD현대미포", "0.4059146"),
    Case("042670", "267270", "2026-01-26", (
        ("krx-daily-2025-09-12-v1.zip", "e21a3df2065aa43853b827a3f72281d59bcad5d22b28d7a6066658bbd6a77f01"),
        ("krx-daily-2026-01-05-v1.zip", "cc413764abe56573d93c1bf8fce62b38deaceb45b0f354bd914afc1bd7603ef6")),
        ("dart-actions-01205842-2025-12-31-2026-01-26-v1.zip", "b5abb975dc126aa68ace4285db33547f31738aac92947067ba92d2d2b8f7a2b6"),
        "20260102000224", "인프라코어", "0.1621707"),
)


def _bar(rows: list[dict], ticker: str) -> dict | None:
    matches = [row for row in rows if row.get("ISU_CD") == ticker]
    if len(matches) > 1:
        raise ValueError("duplicate ticker in KRX session")
    return matches[0] if matches else None


def _traded(row: dict) -> bool:
    fields = [_number(row.get(key)) for key in
              ("TDD_OPNPRC", "TDD_CLSPRC", "ACC_TRDVOL", "ACC_TRDVAL")]
    return all(value is not None and value > 0 for value in fields)


def analyze_price_lineage(days: dict[str, list[dict]], case: Case) -> dict:
    """Separate last executable bar from the KRX's repeated, non-trading close."""
    sessions = sorted(days)
    if case.first_absent not in days:
        raise ValueError("first absent session has no archived KRX response")
    old_rows = [(day, _bar(days[day], case.old_ticker)) for day in sessions
                if day < case.first_absent]
    old_rows = [(day, row) for day, row in old_rows if row is not None]
    if not old_rows or sessions[sessions.index(old_rows[-1][0]) + 1] != case.first_absent:
        raise ValueError("old security disappearance is not adjacent to the archived last row")
    if any(_bar(days[day], case.old_ticker) is not None for day in sessions
           if day >= case.first_absent):
        raise ValueError("old security reappeared after expected disappearance")
    traded_rows = [(day, row) for day, row in old_rows if _traded(row)]
    if not traded_rows:
        raise ValueError("old security has no verified trading bar")
    last_day, last_row = traded_rows[-1]
    stale = [(day, row) for day, row in old_rows if day > last_day]
    if not stale:
        raise ValueError("expected non-trading quote interval is absent")
    last_close = _number(last_row.get("TDD_CLSPRC"))
    for day, row in stale:
        if (day >= case.first_absent or _number(row.get("TDD_OPNPRC")) != 0
                or _number(row.get("ACC_TRDVOL")) != 0
                or _number(row.get("ACC_TRDVAL")) != 0
                or _number(row.get("TDD_CLSPRC")) != last_close):
            raise ValueError("non-trading quote is not a flat, zero-volume carry-forward")
    successor_path = []
    if case.successor_ticker:
        for day in sessions[sessions.index(case.first_absent):]:
            row = _bar(days[day], case.successor_ticker)
            if row is None or not _traded(row):
                raise ValueError("successor first-five-session price path is incomplete")
            successor_path.append({"session": day,
                                   "close_krw": int(_number(row["TDD_CLSPRC"])),
                                   "volume": int(_number(row["ACC_TRDVOL"]))})
            if len(successor_path) == 5:
                break
        if len(successor_path) != 5:
            raise ValueError("fewer than five archived successor trading sessions")
    return {"old_ticker": case.old_ticker, "successor_ticker": case.successor_ticker,
            "last_traded": {"session": last_day, "close_krw": int(last_close),
                            "volume": int(_number(last_row["ACC_TRDVOL"]))},
            "zero_volume_carry": {"start": stale[0][0], "end": stale[-1][0],
                                  "sessions": len(stale), "carried_close_krw": int(last_close)},
            "first_absent_session": case.first_absent,
            "successor_first_five_trading_sessions": successor_path,
            "settlement_verified": False, "total_return_verified": False,
            "strict_pit_eligible": False, "registered_verdict": False}


def _verify_receipt(path: Path, expected_sha: str, case: Case) -> str:
    checked = verify_action_archive(path)
    if checked["source"] != "OpenDART" or checked["sha256"] != expected_sha:
        raise SourceError("price lineage: DART archive identity differs")
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        name = f"raw/doc/{case.receipt}.zip"
        entry = manifest["entries"].get(name)
        if not entry:
            raise SourceError("price lineage: cited DART original is missing")
        with ZipFile(io.BytesIO(archive.read(name))) as document:
            raw = document.read(f"{case.receipt}.xml")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceError("price lineage: DART original encoding differs") from None
    flattened = re.sub(r"<[^>]*>|\s+", " ", text)
    if case.old_issuer_text not in flattened or case.consideration_text not in flattened:
        raise SourceError("price lineage: DART issuer or consideration differs")
    return entry["sha256"]


def review(data_dir: Path) -> dict:
    rows = []
    for case in CASES:
        for filename, expected_sha in case.price_archives:
            if hashlib.sha256((data_dir / filename).read_bytes()).hexdigest() != expected_sha:
                raise ValueError(f"{filename}: frozen KRX archive hash differs")
        days, price_sources = load_krx_archives(
            [data_dir / filename for filename, _ in case.price_archives])
        dart_name, dart_sha = case.dart_archive
        original_sha = _verify_receipt(data_dir / dart_name, dart_sha, case)
        rows.append({**analyze_price_lineage(days, case),
                     "dart_receipt": case.receipt,
                     "dart_original_sha256": original_sha,
                     "dart_archive_sha256": dart_sha,
                     "price_sources": price_sources})
    return {"schema": "research-action-price-lineage-v1", "research_only": True,
            "cases": rows, "all_cases_settlement_verified": False,
            "all_cases_total_return_verified": False,
            "limitations": ["A KRX carry-forward close with zero open/volume/value is not a trade",
                            "The successor close does not prove old-holder share delivery",
                            "Cash exchange and fractional cash remain unverified",
                            "Historical retrieval is not contemporaneous PIT observation"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    args = parser.parse_args()
    print(json.dumps(review(args.data_dir), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
