"""Offline source-gap inventory, development diagnosis only; import is inert.

Consumes the frozen features_at price set without fitting, predicting, labeling,
or replaying a strategy. Potential MA20 consumption is not actual selection.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, is_dataclass
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import signal
import sys
import time

BASE_COMMIT = "ddb8b6ecaad433f8809ce997dd9e67e859996cf4"
SUPPLEMENT_COMMIT = "ae6396eb34edc9c64547a9ad1a00e2b119c3279a"
BASELINE = "data/research/kr-r3-consumed-feature-share-change-inventory-2026-10-18-v1.json"
BASELINE_SHA = "eb542ddc2a1cba9a2c3477b4456ab5bda2698ae32196a9275a5e0a5e3efa2afd"
REPLAY = "data/research/kr-ridge-hold-renewal-diagnostic-2026-10-18-celltrion-kia-v1.json"
REPLAY_SHA = "50b3400dd7e8a3d25224b03d53bef893e8de2bf43b509443285148dc0f6e7299"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def numeric(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def positive(value):
    return value is not None and value > 0


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def history(panel, decision, length):
    if length not in (20, 21, 253):
        raise ValueError("unsupported_history_length")
    i = panel.index[decision]
    result = panel.sessions[max(0, i-length+1):i+1]
    if len(result) != length:
        raise ValueError("incomplete_consumed_history")
    return result


def checked_facts(panel, by_day, reviewed, number=numeric, is_positive=positive):
    """Reconcile every known fact, including facts outside consumed histories."""
    facts = {}
    iterable = reviewed.items() if isinstance(reviewed, dict) else reviewed
    for key, fact in iterable:
        f = asdict(fact) if is_dataclass(fact) else dict(fact)
        day, ticker = key
        i = panel.index[day]
        previous = panel.sessions[i-1] if i > 0 else None
        before = number(by_day.get(previous, {}).get(ticker, {}).get("LIST_SHRS"))
        after = number(by_day.get(day, {}).get(ticker, {}).get("LIST_SHRS"))
        if (f.get("ticker") != ticker or f.get("listing_session") != day
                or f.get("previous_session") != previous
                or f.get("holder_quantity_effect") != "unchanged"
                or not is_positive(before) or not is_positive(after)
                or f.get("listed_shares_before") != before
                or f.get("listed_shares_after") != after
                or not f.get("source_url") or not f.get("source_sha256")):
            raise ValueError("known_fact_value_or_source_mismatch:" + str(key))
        # Keep only JSON-safe proof fields, not observation datetime objects.
        proof = {k: f[k] for k in ("ticker", "listing_session", "previous_session",
                 "holder_quantity_effect", "listed_shares_before", "listed_shares_after",
                 "source_url", "source_sha256")}
        if key in facts and facts[key] != proof:
            raise ValueError("contradictory_source_for_key:" + str(key))
        facts[key] = proof
    return facts


def aggregate(panel, by_day, reviewed, consumers, *, check=lambda: None,
              number=numeric, is_positive=positive):
    """Pure aggregation of explicit consumers. No I/O, prediction or mutation.

    Consumer fields: period, decision_session, ticker, layer, window (20/21/253),
    optional path_reference. Key identity always includes the previous session.
    """
    facts = checked_facts(panel, by_day, reviewed, number, is_positive)
    entries, missing, seen, gates = {}, {}, set(), {}
    price_consumers, decisions = set(), set()
    metadata_consumptions = set()
    for consumer in consumers:
        check()
        token = canonical(consumer)
        if token in seen:
            continue
        seen.add(token)
        c = dict(consumer)
        period, day, ticker = c["period"], c["decision_session"], c["ticker"]
        layer = c["layer"]
        dates = history(panel, day, c["window"])
        if layer in ("features", "market-basket"):
            price_consumers.add((period, day, ticker))
            decisions.add((period, day))
            gates.setdefault((period, day), None)
        for previous, current in zip(dates, dates[1:]):
            before = number(by_day.get(previous, {}).get(ticker, {}).get("LIST_SHRS"))
            after = number(by_day.get(current, {}).get(ticker, {}).get("LIST_SHRS"))
            key = (ticker, previous, current)
            absent = not is_positive(before) or not is_positive(after)
            if not absent and before == after:
                continue
            store = missing if absent else entries
            if key not in store:
                fact = facts.get((current, ticker)) if not absent else None
                store[key] = {"ticker": ticker, "previous_session": previous,
                    "session": current, "listed_shares_before": before,
                    "listed_shares_after": after,
                    "source_ids": [f"preserved-raw:{previous}:{ticker}",
                                   f"preserved-raw:{current}:{ticker}"],
                    "verified_event_key": fact is not None,
                    "effect_class": "missing_metadata" if absent else (
                        "reviewed_holder_quantity_unchanged" if fact else "unknown_do_not_infer_from_count"),
                    "reviewed_proof": fact, "layers": set(), "consumed_by": {}}
            row = store[key]
            row["layers"].add(layer)
            row["consumed_by"][token] = c
            if absent:
                metadata_consumptions.add((key, period, day, ticker))
            elif not row["verified_event_key"] and layer == "features":
                # Same ordered first-stop traversal as feature_history_issue:
                # original prices insertion order, then chronological pairs.
                if gates.get((period, day)) is None:
                    gates[period, day] = {"ticker": ticker, "previous_session": previous,
                                         "session": current}
    def encode(store):
        return [{**row, "layers": sorted(row["layers"]),
                 "consumed_by": sorted(row["consumed_by"].values(), key=canonical)}
                for _, row in sorted(store.items())]
    encoded = encode(entries)
    feature = [e for e in encoded if "features" in e["layers"]]
    unknown = [e for e in feature if not e["verified_event_key"]]
    return {"entries": encoded, "missing_metadata_pairs": encode(missing),
        "unreviewed": [e for e in encoded if not e["verified_event_key"]],
        "daily_earliest_gates": [{"period": p, "decision_session": d,
            "first_feature_gate": g} for (p, d), g in sorted(gates.items())],
        "feature_summary": {"decision_dates": len(decisions),
            "price_consumer_ticker_dates": len(price_consumers),
            "unique_observed_change_keys": len(feature),
            "reviewed_consumed_keys": sum(e["verified_event_key"] for e in feature),
            "unreviewed_keys": len(unknown),
            "unreviewed_tickers": len({e["ticker"] for e in unknown}),
            "missing_metadata_pair_consumers": len(metadata_consumptions)},
        "reviewed_fact_count": len(facts), "reviewed_fact_proof": list(facts.values()),
        "unique_consumers_with_layer": len(seen)}


def reported_paths(saved):
    """Read preserved trace/prefix only; null arms never imply execution."""
    out = []
    for path in saved["paths"]:
        for arm in ("forced", "net", "candidate"):
            reached, not_reached, holdings = [], [], []
            for row in path["trace"]:
                value = (row.get("arms") or {}).get(arm)
                if value is None:
                    not_reached.append(row["session"])
                else:
                    reached.append(row["session"])
                    holdings.append({"session": row["session"],
                                     "holdings": value.get("holdings", "unknown")})
            prefix = path.get("confirmed_execution_prefix", {}).get(arm, {})
            out.append({"path_reference": {k: path[k] for k in ("period", "cost", "phase")}
                | {"arm": arm}, "stop": path.get("stop"),
                "reached_sessions": reached, "not_reached_sessions": not_reached,
                "holdings_reported": holdings,
                "fills_reported": prefix.get("fills", "unknown"),
                "pending_orders_reported": prefix.get("pending", "unknown"),
                "submissions_reported": path.get("submissions", "unknown"),
                "unreached_holdings_order_coverage": "unknown/not_reached",
                "scope": "reported_prefix_only_no_replay"})
    return out


def load_runner(root):
    path = root / "scripts/measure/kr_ridge_hold_renewal_diagnostic.py"
    spec = importlib.util.spec_from_file_location("inventory_frozen_runner", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def checked_json(root, name, sha):
    if digest(root / name) != sha:
        raise ValueError("preserved_result_hash_mismatch:" + name)
    return json.loads((root / name).read_text())


def diagnose(args, runner, budget, counters):
    root = args.repo.resolve()
    import subprocess
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                   timeout=10).decode().strip()
    # Preserve the audited inputs, while allowing this tool's own later commit.
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", BASE_COMMIT, head],
                              cwd=root, timeout=10, capture_output=True)
    if ancestor.returncode != 0:
        raise ValueError("declared_repository_base_not_ancestor")
    contract, hashes = runner.checked_contract(root, runner.CONTRACT_COMMIT, budget)
    baseline = checked_json(root, BASELINE, BASELINE_SHA)
    saved = checked_json(root, REPLAY, REPLAY_SHA)
    proof = {}
    if args.source_supplement:
        if args.supplement_commit != SUPPLEMENT_COMMIT:
            raise ValueError("required_supplement_precommit_differs")
        _, proof["hyundai"] = runner.source_supplement_module().checked_spec(root,
            args.source_supplement, args.source_supplement_archive, args.supplement_commit,
            runner.CONTRACT_COMMIT, runner.CONTRACT_SHA, hashes, budget)
    if args.event_supplement:
        if args.event_supplement_commit != SUPPLEMENT_COMMIT:
            raise ValueError("required_event_precommit_differs")
        _, proof["celltrion_kia"] = runner.event_supplement_module().checked_spec(root,
            args.event_supplement, args.event_supplement_commit, runner.CONTRACT_COMMIT,
            runner.CONTRACT_SHA, hashes, budget)
    modules = runner.dependencies(root)
    def forbid(name):
        def forbidden(*unused, **kw):
            key = {"fit": "fit_calls", "predict": "predict_calls", "label_at": "label_calls"}.get(name,
                "strategy_replay_calls")
            counters[key] += 1
            raise ValueError("forbidden_call:" + name)
        return forbidden
    # Fail closed even if a future refactor accidentally calls these entry points.
    for module in (runner, *modules.values()):
        for name in ("prepare", "fit", "predict", "label_at", "replay", "replay_pair", "execute"):
            if hasattr(module, name):
                setattr(module, name, forbid(name))
    lab = modules["lab"]
    for path in (lab.FINANCIAL, lab.SCOPE):
        if not Path(path).resolve().is_relative_to(root):
            raise ValueError("financial_binding_outside_repo")
    archives = [root / contract["inputs"]["priceArchivesUnder"] / n
                for n in contract["inputs"]["priceArchiveSHA256"]]
    days, sources = lab.load_krx_archives(archives)
    budget.check()
    panel = modules["panel"].PricePanel(days)
    by_day = {d: {r["ISU_CD"]: r for r in rows} for d, rows in days.items()}
    old_result = json.loads((root / modules["old"].RESULT).read_text())
    reviewed = modules["old"].checked_reviewed(root, old_result, by_day, panel)
    counts = [len(reviewed)]
    from signal_desk.signals.research_action_coverage import ReviewedShareCountChange
    if args.source_supplement:
        reviewed = runner.source_supplement_module().merge_archive(
            args.source_supplement_archive, reviewed, by_day, panel, ReviewedShareCountChange)
        counts.append(len(reviewed))
    if args.event_supplement:
        reviewed = runner.event_supplement_module().merge(
            root, reviewed, by_day, panel, ReviewedShareCountChange)
        counts.append(len(reviewed))
    if counts != ([8, 9, 12] if args.event_supplement else [8, 9] if args.source_supplement else [8]):
        raise ValueError("reviewed_8_plus_1_plus_3_reconciliation_failed")
    financials = lab.load_financials(lab.FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in lab.top200(days[contract["inputs"]["financial"]["rosterAsOf"]])}
    masks, consumers = [], []
    for period in contract["periods"]:
        dates = runner.sessions_between(modules["clock"], period["decisionStart"], period["decisionEnd"])
        if period.get("expectedSessions") and len(dates) != period["expectedSessions"]:
            raise ValueError("decision_calendar_count")
        for day in dates:
            budget.check()
            rows, prices, _, universe, excluded = lab.features_at(day, panel, by_day, financials, roster)
            masks.append({"period": period["id"], "decision_session": day,
                "top200": [r["ISU_CD"] for r in universe], "price_eligible": list(prices),
                "feature_row_tickers": [r["ticker"] for r in rows], "financial_excluded": excluded})
            for ticker in prices:
                # Eligibility and the original unit guard use 253 closes;
                # market20 itself directly uses only closes[-21:] (20 returns).
                for layer, window in (("features", 253), ("market-basket", 21), ("potential_trend", 20)):
                    consumers.append({"period": period["id"], "decision_session": day,
                        "ticker": ticker, "layer": layer, "window": window})
    result = aggregate(panel, by_day, reviewed, consumers, check=budget.check,
                       number=runner.number, is_positive=runner.positive)
    summary = result["feature_summary"]
    fields = ("decision_dates", "price_consumer_ticker_dates", "unique_observed_change_keys",
              "reviewed_consumed_keys", "unreviewed_keys", "unreviewed_tickers",
              "missing_metadata_pair_consumers")
    comparison = {k: {"baseline": baseline[k], "actual": summary[k],
                      "matches": baseline[k] == summary[k]} for k in fields}
    baseline_keys = {(e["ticker"], e["previous_session"], e["session"]):
                     {(c["period"], c["decision_session"]) for c in e["consumed_by"]}
                     for e in baseline["entries"]}
    actual_keys = {(e["ticker"], e["previous_session"], e["session"]):
                  {(c["period"], c["decision_session"]) for c in e["consumed_by"] if c["layer"] == "features"}
                  for e in result["entries"] if "features" in e["layers"]}
    exact_match = baseline_keys == actual_keys
    if not exact_match or any(not comparison[k]["matches"] for k in fields if k not in (
            "reviewed_consumed_keys", "unreviewed_keys", "unreviewed_tickers")):
        raise ValueError("baseline_feature_consumption_mismatch")
    if args.event_supplement and not all(v["matches"] for v in comparison.values()):
        raise ValueError("full_supplement_baseline_mismatch")
    reports = reported_paths(saved)
    if len(reports) != 60:
        raise ValueError("reported_account_path_count_not_60")
    if summary["decision_dates"] != 119:
        raise ValueError("total_decision_count_not_119")
    for entry in result["entries"]:
        entry["reported_stop_path_references"] = [r["path_reference"] for r in reports
            if (r.get("stop") or {}).get("ticker") == entry["ticker"]
            and (r.get("stop") or {}).get("evidence_session", (r.get("stop") or {}).get("session")) == entry["session"]]
    return {**result, "input_sha256": hashes, "contract_commit": runner.CONTRACT_COMMIT,
        "contract_sha256": runner.CONTRACT_SHA, "repository_head": head,
        "audited_base_commit": BASE_COMMIT,
        "runner_sha256": digest(runner.__file__), "supplement_proof": proof,
        "reviewed_count_stages": counts, "sources": sources, "daily_masks": masks,
        "financial_roster": sorted(roster), "baseline_sha256": BASELINE_SHA,
        "baseline_comparison": comparison, "baseline_exact_keys_and_consumers_match": exact_match,
        "reported_replay_sha256": REPLAY_SHA, "reported_paths": reports,
        "price_replay_feature_gate_clear": not any(
            row["first_feature_gate"] for row in result["daily_earliest_gates"]),
        "ready": False, "blockers": ["unreviewed_keys_and_missing_metadata_see_complete_lists",
            "unreached_holdings_and_orders_unknown", "cash_dividends_rights_effective_dates_corrections_not_certified",
            "registered_210_paths_remain_source_blocked"],
        "common_evidence_needed": ["exact_ticker_previous_session_session_originals_and_corrections",
            "holder_quantity_price_unit_cash_rights_and_settlement_mapping",
            "complete_source_coverage_then_separately_authorized_replay"],
        "certification": {k: "not_certified" for k in (
            "cash_dividends", "rights", "effective_dates", "corrections", "registered_210_paths", "total_return")},
        "scope": "two frozen periods / 119 decisions; reused development data; potential_trend is not holdings or selection"}


def parser_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-supplement", type=Path)
    parser.add_argument("--source-supplement-archive", type=Path)
    parser.add_argument("--supplement-commit")
    parser.add_argument("--event-supplement", type=Path)
    parser.add_argument("--event-supplement-commit")
    args = parser.parse_args(argv)
    supplied = [args.source_supplement, args.source_supplement_archive, args.supplement_commit]
    if any(supplied) and not all(supplied):
        parser.error("supplement requires all three source options")
    event = [args.event_supplement, args.event_supplement_commit]
    if any(event) and (not all(event) or not all(supplied)):
        parser.error("event supplement requires both event and all source options")
    return args


def main(argv=None):
    args = parser_args(argv)
    started, cpu = time.monotonic(), time.process_time()
    counters = {k: 0 for k in ("fit_calls", "predict_calls", "label_calls", "strategy_replay_calls", "source_requests")}
    def audit(event, unused):
        if event.startswith("socket."):
            counters["source_requests"] += 1
            raise PermissionError("network_blocked_by_inventory_audit")
    # Installed only at CLI execution, before loading repository modules.
    sys.addaudithook(audit)
    def timeout(*unused):
        raise TimeoutError("600_second_cpu_or_wall_limit")
    old_handlers = {s: signal.signal(s, timeout) for s in (signal.SIGALRM, signal.SIGPROF)}
    signal.setitimer(signal.ITIMER_REAL, 600)
    signal.setitimer(signal.ITIMER_PROF, 600)
    result = {"schema": "kr-r3-source-inventory-v1", "grade": "development_diagnostic", "ready": False}
    try:
        # Exclusive reservation occurs before any preserved history is read.
        with args.output.open("x", encoding="utf-8") as stream:
            try:
                runner = load_runner(args.repo.resolve())
                result.update(diagnose(args, runner, runner.Budget(), counters))
                result["state"] = "inventory_complete"
            except Exception as exc:
                result.update(state="implementation_failure", reason=f"{type(exc).__name__}:{exc}")
            result.update(counters)
            result["tool_sha256"] = digest(__file__)
            result["elapsed_seconds"] = {"wall": time.monotonic()-started, "cpu": time.process_time()-cpu}
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        print(json.dumps({k: result[k] for k in ("state", "ready", "elapsed_seconds")}))
        return 0 if result["state"] == "inventory_complete" else 2
    except Exception as exc:
        print(json.dumps({"state": "implementation_failure", "reason": f"{type(exc).__name__}:{exc}"}), file=sys.stderr)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_PROF, 0)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
