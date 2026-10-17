"""Original-source readiness census, never a substitute price-only backtest."""

from __future__ import annotations

from collections import Counter
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from statistics import median

from scripts.measure.acquire_historical_sources import (
    compare_dart_kis_dividend, extract_dart_common_dividend,
    source_window_from_archive, verify_action_archive,
)
from scripts.measure.kr_net_alpha_sources import DEST, ROOT, digest, number, plan, price_inventory
from signal_desk.signals.research_action_coverage import ACTION_KINDS, CoverageCertificate


def action_sources():
    """Inventory preserved, overlapping KIS/DART scope, including earlier work."""
    sources, windows = [], []
    for path in sorted((ROOT/"data/research").glob("*.zip")):
        from zipfile import ZipFile
        with ZipFile(path) as z:
            if "manifest.json" not in z.namelist():
                continue
            m = json.loads(z.read("manifest.json"))
        if (m.get("schema") != "historical-action-source-v1"
                or m.get("source") not in ("OpenDART", "KIS KSD schedule")
                or not m.get("start") or not m.get("end")
                or m["end"] < "2020-01-01" or m["start"] > "2024-01-31"):
            continue
        checked = verify_action_archive(path)
        window = source_window_from_archive(path)
        windows.append(window)
        sources.append({"archive": path.name, "sha256": checked["sha256"],
            "request_count": m["request_count"], "window": asdict(window),
            "total_return_coverage": False})
    return sources, windows


def audit(output=None):
    spec = plan()
    universes_path = DEST/"universes.json"
    original = universes_path.read_bytes()
    universes = json.loads(original)
    if universes["plan_sha256"] != digest((ROOT/"docs/kr-net-alpha-plan-2026-10-17.json").read_bytes()):
        raise ValueError("universe/plan differs")
    days, sources, missing = price_inventory()
    if missing:
        raise ValueError("readiness inventory requires complete price calendar")
    ordered = sorted(days)
    indices = {day: i for i, day in enumerate(ordered)}
    input_rows, label_price_gaps, roster = [], [], set()
    for u in universes["dates"]:
        date, i = u["date"], indices[u["date"]]
        eligible = []
        for s in u["securities"]:
            t = s["ticker"]; roster.add(t)
            history = [days[d].get(t, {}) for d in ordered[max(0, i-252):i+1]]
            closes = [number(r.get("TDD_CLSPRC")) for r in history]
            vols = [number(r.get("ACC_TRDVOL")) for r in history[-21:]]
            if (len(closes) == 253 and all(v is not None and v > 0 for v in closes)
                    and len(vols) == 21 and all(v is not None and v >= 0 for v in vols)
                    and sum(vols[:-1]) > 0):
                eligible.append(t)
        raw_label_prices_complete = bool(eligible)
        for t in eligible:
            for offset in (1, 1+spec["label_horizon_sessions"]):
                if i+offset >= len(ordered):
                    raise ValueError("fixed label tail unavailable")
                px = number(days[ordered[i+offset]].get(t, {}).get("TDD_OPNPRC"))
                if px is None or px <= 0:
                    raw_label_prices_complete = False
                    label_price_gaps.append({"date": date, "ticker": t,
                        "missing_open_session": ordered[i+offset]})
        # Source-quality diagnostic only. Do not silently change frozen U_t.
        active_today = [t for t in eligible if (number(days[date][t]["TDD_OPNPRC"]) or 0) > 0
                        and (number(days[date][t]["ACC_TRDVOL"]) or 0) > 0]
        active_pairs = bool(active_today) and all(
            (number(days[ordered[i+offset]].get(t, {}).get("TDD_OPNPRC")) or 0) > 0
            for t in active_today for offset in (1, 1+spec["label_horizon_sessions"]))
        input_rows.append({"date": date, "raw_history_names": len(eligible),
            "raw_price_label_pairs_complete": raw_label_prices_complete,
            "diagnostic_known_active_names": len(active_today),
            "diagnostic_known_active_open_pairs_complete": active_pairs,
            "source_adjusted_feature_ready": False, "economic_label_ready": False})
    source_rows, windows = action_sources()
    phases = {}
    for phase, start, end, labels_through, minimum in (
        ("train", spec["train_start"], spec["train_end"], spec["train_labels_through"], spec["min_complete_train_dates"]),
        ("calibration", spec["calibration_start"], spec["calibration_end"], spec["calibration_labels_through"], spec["min_complete_calibration_dates"]),
        ("evaluation", spec["evaluation_start"], spec["evaluation_end"], spec["liquidation_through"], spec["min_evaluation_sessions"]),
    ):
        rows = [r for r in input_rows if start <= r["date"] <= end]
        states = Counter()
        for ticker in sorted(roster):
            cert = CoverageCertificate("kr", f"ticker:{ticker}", start, labels_through,
                ACTION_KINDS-{"unsupported"}, tuple(windows))
            states[cert.state] += 1
        phases[phase] = {"decision_dates": len(rows),
            "raw_history_names_min": min(r["raw_history_names"] for r in rows),
            "raw_history_names_median": median(r["raw_history_names"] for r in rows),
            "raw_history_dates_ge160": sum(r["raw_history_names"] >= spec["min_feature_eligible"] for r in rows),
            "raw_open_pairs_dates_ge160": sum(r["raw_history_names"] >= spec["min_feature_eligible"]
                 and r["raw_price_label_pairs_complete"] for r in rows),
            "diagnostic_known_active_open_pairs_dates_ge160": sum(
                r["diagnostic_known_active_names"] >= spec["min_feature_eligible"]
                and r["diagnostic_known_active_open_pairs_complete"] for r in rows),
            "action_scope_states_over_roster": dict(states), "required_dates": minimum,
            "verified_economic_label_dates": 0,
            "economic_outcome_bundle_present": False}
    queue = []
    for previous, current in zip(ordered, ordered[1:]):
        for ticker in sorted(roster & set(days[current]) & set(days[previous])):
            a = number(days[previous][ticker]["LIST_SHRS"])
            b = number(days[current][ticker]["LIST_SHRS"])
            if a is not None and b is not None and a != b:
                queue.append({"ticker": ticker, "name": days[current][ticker]["ISU_NM"],
                    "previous_session": previous, "session": current,
                    "listed_shares_before": a, "listed_shares_after": b,
                    "state": "unclassified_share_count_change_not_inferred_split"})
    fact = extract_dart_common_dividend(ROOT/"data/research/kr-net-alpha-dart-005930-2021-04-original-v1.zip", "20210429800081")
    comparison = compare_dart_kis_dividend(fact, ROOT/"data/research/kr-net-alpha-kis-dividend-005930-2021-03-01-v1.zip")
    journal = json.loads((DEST/"requests.json").read_text())
    new_actions = [r for r in source_rows if r["archive"].startswith("kr-net-alpha-")]
    halt_path = ROOT/"data/research/kr-net-alpha-kind-asiana-halt-2021-v1.zip"
    halt_case = verify_action_archive(halt_path)
    payload = {"schema": "kr-net-alpha-readiness-v1", "candidate_id": spec["candidate_id"],
        "plan_commit": "8c6d837a91f856ade03b75bdaec672abd51e051d",
        "plan_sha256": universes["plan_sha256"], "universe_sha256": digest(original),
        "price_sources": sources, "price_sessions": len(ordered), "missing_price_sessions": missing,
        "price_metadata_requests": len(journal), "request_states": dict(Counter(r["state"] for r in journal)),
        "new_action_requests": 1+sum(r["request_count"] for r in new_actions),
        "identity_dates": len(universes["dates"]), "roster_tickers": len(roster), "phases": phases,
        "action_sources": source_rows, "samsung_dividend_facts_comparison": comparison,
        "asiana_known_halt_original": {"archive": halt_path.name, **halt_case},
        "share_count_review_queue": queue, "label_price_gaps": label_price_gaps,
        "input_dates": input_rows, "state": "blocked_data", "fit_executed": False,
        "backtest_executed": False, "adoption": "not_evaluable_data_gate",
        "blockers": ["effective_date_action_scope_not_certified",
            "lookback_action_unit_adjustments_not_verified", "verified_total_return_outcome_bundle_missing",
            *(["frozen_raw_universe_train_open_pairs_below_minimum"] if
                phases["train"]["raw_open_pairs_dates_ge160"] < spec["min_complete_train_dates"] else [])],
        "live_order_eligible": False, "registered_verdict": False, "integrated_progress": 40}
    output = Path(output) if output is not None else DEST/"readiness-v3.json"
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
        default=lambda value: sorted(value) if isinstance(value, (set, frozenset)) else value.isoformat())
    if output.exists() and output.read_text() != encoded:
        raise ValueError("readiness evidence differs; use a new --output path")
    output.write_text(encoded)
    print(json.dumps({k: payload[k] for k in ("state", "price_sessions", "price_metadata_requests", "new_action_requests", "identity_dates", "roster_tickers", "phases", "blockers")}, ensure_ascii=False))
    print("sha256", digest(output.read_bytes()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    audit(parser.parse_args().output)
