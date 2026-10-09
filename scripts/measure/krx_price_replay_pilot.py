"""Offline C-level price-only replay from frozen KRX raw response archives.

Uses each decision date's KOSPI market-cap top 200 (not the KOSPI200 index),
the unchanged common engine, and the existing next-open/h-close case audit.
This is an exploratory development diagnostic, never a registered verdict or
the historical 8-factor/live-order decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from signal_desk import market_clock  # noqa: E402
from signal_desk.signals import historical_audit, historical_replay  # noqa: E402
from signal_desk.signals.engine import SignalConfig  # noqa: E402

from scripts.measure.acquire_historical_sources import _registered_start  # noqa: E402


def _number(value: object) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def load_krx_archives(paths: list[Path]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Verify every raw response against its manifest; never trust only a file name."""
    if not paths:
        raise ValueError("at least one frozen KRX archive is required")
    days, sources = {}, []
    for path in paths:
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if (manifest.get("schema") != "historical-source-v1"
                    or manifest.get("endpoint") != "sto/stk_bydd_trd"
                    or manifest.get("market") != "kr"):
                raise ValueError(f"{path.name}: not a frozen KRX daily archive")
            entries = manifest.get("entries", {})
            expected = set(manifest.get("expected_sessions", []))
            if not expected or {name.removeprefix("raw/").removesuffix(".json")
                                for name in entries} != expected:
                raise ValueError(f"{path.name}: manifest session mismatch")
            for name, entry in entries.items():
                raw = archive.read(name)
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError(f"{path.name}: raw response digest mismatch")
                session = entry["session"]
                body = json.loads(raw)
                rows = body.get("OutBlock_1")
                if (session in days or not isinstance(rows, list) or len(rows) != entry["rows"]
                        or any(row.get("BAS_DD") != session.replace("-", "") for row in rows)):
                    raise ValueError(f"{path.name}: duplicate or malformed daily rows")
                days[session] = rows
        sources.append({"archive": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "sessions": len(expected)})
    first, last = min(days), max(days)
    current = dt.date.fromisoformat(first)
    while current <= dt.date.fromisoformat(last):
        session = current.isoformat()
        if market_clock.is_session("kr", current) and session not in days:
            raise ValueError(f"missing KRX session {session}; no interpolation")
        current += dt.timedelta(days=1)
    return days, sources


def run_pilot(days: dict[str, list[dict]], *, start: str, end: str) -> dict:
    if start > end or end >= _registered_start():
        raise ValueError("replay decisions must end before the registered period")
    ordered = sorted(days)
    decisions = [day for day in ordered if start <= day <= end]
    if not decisions or len(decisions) > 60:
        raise ValueError("pilot needs 1–60 covered decision sessions")
    cfg = SignalConfig()
    warmup = max(cfg.ma_long, cfg.momentum_lookback + 1)
    index = {day: i for i, day in enumerate(ordered)}
    bars = []
    histories: dict[str, list[tuple[str, float]]] = defaultdict(list)
    invalid_price_rows = 0
    for day in ordered:
        for row in days[day]:
            ticker = str(row.get("ISU_CD") or "")
            open_px, close_px = _number(row.get("TDD_OPNPRC")), _number(row.get("TDD_CLSPRC"))
            if len(ticker) != 6:
                continue
            bars.append({"date": day, "ticker": ticker, "open": open_px, "close": close_px})
            if close_px is not None and close_px > 0:
                histories[ticker].append((day, close_px))
            else:
                invalid_price_rows += 1
    closes_by = {ticker: [close for _, close in rows] for ticker, rows in histories.items()}
    dates_by = {ticker: [day for day, _ in rows] for ticker, rows in histories.items()}
    output_rows, reasons, no_warmup = [], defaultdict(int), 0
    for day in decisions:
        candidates = []
        for row in days[day]:
            ticker = str(row.get("ISU_CD") or "")
            cap = _number(row.get("MKTCAP"))
            if len(ticker) == 6 and ticker.endswith("0") and cap is not None and cap > 0:
                candidates.append((cap, ticker, str(row.get("ISU_NM") or ticker)))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        universe = [{"ticker": ticker, "name": name} for _, ticker, name in candidates[:200]]
        if len(universe) != 200:
            raise ValueError(f"{day}: fewer than 200 valid common-share candidates")
        # A missing price bar in the required lookback is not a shorter horizon.
        excluded = set()
        for item in universe:
            ticker = item["ticker"]
            dates = dates_by.get(ticker, [])
            prefix = [d for d in dates if d <= day]
            if (len(prefix) < warmup or prefix[-1] != day
                    or [index[d] for d in prefix[-warmup:]]
                    != list(range(index[day] - warmup + 1, index[day] + 1))):
                excluded.add(ticker)
        no_warmup += len(excluded)
        replay = historical_replay.replay_price_only_asof(
            market="kr", as_of=day, universe=universe,
            closes_by={t: values for t, values in closes_by.items() if t not in excluded},
            dates_by={t: values for t, values in dates_by.items() if t not in excluded},
            config=cfg)
        for reason in replay["excluded_tickers"].values():
            reasons[reason] += 1
        output_rows.extend({"date": day, "ticker": row["ticker"],
                            "score": row["score"], "kind": row["kind"]}
                           for row in replay["rows"])
    if not output_rows:
        raise ValueError("no replay signals; do not manufacture zero-return periods")
    audit = historical_audit.audit_snapshots(
        pd.DataFrame(output_rows), pd.DataFrame(bars), market="kr", include_aggregates=True)
    audit["source_level"] = "C_offline_price_only_current_engine"
    audit["strict_pit_eligible"] = False
    audit["registered_verdict"] = False
    audit["live_eligible"] = False
    audit["diagnostic"] = {"decision_start": start, "decision_end": end,
                           "decision_sessions": len(decisions), "warmup_sessions": warmup,
                           "candidate_slots": 200 * len(decisions),
                           "excluded_for_noncontinuous_warmup": no_warmup,
                           "engine_exclusion_reasons": dict(sorted(reasons.items())),
                           "invalid_close_rows_in_raw_panel": invalid_price_rows,
                           "bar_count": len(bars)}
    audit["limitations"] = ["retrieved historical prices are not their original observed vintages",
                            "daily cap top 200 is not the KOSPI200 constituent history",
                            "fundamentals, flow, short, qualitative and execution gates are absent",
                            "raw prices omit unverified dividends/corporate actions",
                            "overlapping ticker-days and held positions are not independent trades"]
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; pilot never overwrites an earlier result")
    days, sources = load_krx_archives(args.archives)
    result = run_pilot(days, start=args.start, end=args.end)
    result["sources"] = sources
    result["code_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                             for name in ("scripts/measure/krx_price_replay_pilot.py",
                                          "src/signal_desk/signals/historical_replay.py",
                                          "src/signal_desk/signals/historical_audit.py",
                                          "src/signal_desk/signals/engine.py")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "source_level": result["source_level"],
                      "decision_sessions": result["diagnostic"]["decision_sessions"],
                      "signal_rows": result["signal_rows"], "registered_verdict": False},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
