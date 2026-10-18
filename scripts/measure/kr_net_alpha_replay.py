"""Source-gated rights/account replay; never fetches data or opens live orders."""

import argparse
import gzip
import json
from pathlib import Path

from scripts.measure import kr_net_alpha_v2 as audit
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest, price_inventory
from signal_desk.signals import research_net_alpha_v2 as v2
from signal_desk.signals.research_net_alpha_execution import path_grid
from signal_desk.signals.research_net_alpha_replay import run_path, sessions


def preflight(path, expected_sha, packet, model_path=None):
    spec = audit.contract()
    raw = Path(path).read_bytes()
    if digest(raw) != expected_sha:
        raise ValueError("readiness artifact hash differs")
    body = json.loads(gzip.decompress(raw) if Path(path).suffix == ".gz" else raw)
    if (body.get("schema") != "kr-net-alpha-v2-readiness-v2"
            or body.get("candidate_id") != spec["candidate_id"]
            or body.get("contract_hashes") != spec["contract_hashes"]
            or body.get("evidence_packet_sha256") != digest(Path(packet).read_bytes())
            or body.get("roster_sha256") != audit.ROSTER_SHA
            or digest((DEST/"universes.json").read_bytes()) != audit.ROSTER_SHA):
        raise ValueError("readiness contract, roster or evidence differs; rebuild audit")
    required_builders = {"scripts/measure/kr_net_alpha_v2.py",
        "src/signal_desk/signals/research_net_alpha_v2.py",
        "src/signal_desk/signals/research_net_alpha.py",
        "src/signal_desk/signals/research_action_clocks.py"}
    builders = body.get("implementation_sha256", {})
    if set(builders) != required_builders or any(digest((ROOT/p).read_bytes()) != sha for p,sha in builders.items()):
        raise ValueError("readiness builder changed; rebuild audit")
    reviewed = audit.evidence(packet)  # Revalidate original source spans and reconciliation inventory.
    rows = body["rows"]
    roster = json.loads((DEST/"universes.json").read_bytes())
    if [r["date"] for r in rows] != [r["date"] for r in roster["dates"]]:
        raise ValueError("readiness calendar differs from fixed roster")
    for row in rows:
        if row["snapshot"]["date"] != row["date"] or v2.sample(row["snapshot"], row["labels"]) != row["sample"]:
            raise ValueError("readiness sample admission differs")
    status = v2.readiness(rows, spec)
    result = {"schema":"kr-net-alpha-account-replay-v1", "candidate_id":spec["candidate_id"],
        "contract_hashes":spec["contract_hashes"], "readiness_sha256":expected_sha,
        "evidence_packet_sha256":digest(Path(packet).read_bytes()), "phases":status["phases"],
        "state":"blocked_data" if status["state"] == "blocked_data" else "blocked_model",
        "source_certificates":len(reviewed.certificates),
        "expected_paths":len(path_grid(spec)), "paths_executed":0, "paths":[],
        "backtest_executed":False, "fit_executed":False, "net_return":None,
        "comparison_complete":False, "adoption":"not_evaluable_data_gate",
        "live_order_eligible":False, "strict_pit_eligible":False, "integrated_progress":40,
        "input_gap_counts":body["input_gap_counts"],
        "implementation_sha256":{p:digest((ROOT/p).read_bytes()) for p in (
            "scripts/measure/kr_net_alpha_replay.py",
            "src/signal_desk/signals/research_net_alpha_account.py",
            "src/signal_desk/signals/research_net_alpha_execution.py",
            "src/signal_desk/signals/research_net_alpha_replay.py")}}
    model = None
    if status["state"] != "blocked_data" and model_path is not None:
        encoded = Path(model_path).read_bytes()
        model = json.loads(encoded)
        if (not body.get("fit_executed") or body.get("model_sha256") != digest(encoded)
                or model.get("schema") != "kr-net-alpha-v2-model-v1"
                or model.get("contract_hashes") != spec["contract_hashes"]
                or model.get("evidence_packet_sha256") != result["evidence_packet_sha256"]):
            raise ValueError("one-fit model provenance differs")
        result.update(state="ready_for_revalidation", model_sha256=digest(encoded))
    return spec, result, model


def run(output, readiness, readiness_sha, packet, model_path=None):
    spec, result, model = preflight(readiness, readiness_sha, packet, model_path)
    # Historical gaps are a terminal data gate, not evidence that 210 cash
    # policies succeeded. No repeated fitting or fabricated return rows.
    if result["state"] == "ready_for_revalidation":
        fresh_path = Path(str(output)+".source-audit.json.gz")
        fresh = audit.run(fresh_path, packet)
        result["revalidation_sha256"] = digest(fresh_path.read_bytes())
        if fresh["state"] != "ready_for_fit":
            result.update(state="blocked_data", phases=fresh["phases"])
        else:
            prices, sources, missing = price_inventory()
            if missing or sources != fresh["price_sources"]:
                raise ValueError("revalidated market inventory differs")
            identities, _ = audit.identities(prices)
            dates = sessions(spec["evaluation_start"], spec["liquidation_through"])
            snapshots = {r["date"]:r["snapshot"] for r in fresh["rows"]}
            bars, ids = {}, {}
            for day in dates:
                raw = prices[day]
                ids[day] = digest(json.dumps({"day":day, "rows":raw,
                    "evidence":result["evidence_packet_sha256"]}, sort_keys=True).encode())
                # Volume is used at the completed close only, not to size an
                # earlier opening order. Zero-volume reference prices are not marks.
                traded = {t:r for t,r in raw.items() if (v2.number(r.get("ACC_TRDVOL")) or 0) > 0}
                bars[day] = {"opens":{t:{"price":v2.number(r.get("TDD_OPNPRC")),
                    "open_tradable":t in traded, "source_id":ids[day]} for t,r in raw.items()},
                    "closes":{t:v2.number(r.get("TDD_CLSPRC")) for t,r in traded.items()},
                    "volumes":{t:v2.number(r.get("ACC_TRDVOL")) for t,r in raw.items()},
                    "caps":{t:v2.number(raw[t].get("MKTCAP")) for t in snapshots.get(day, {}).get("tickers", [])}}
            reviewed = audit.evidence(packet)
            paths = [run_path(spec, scenario, arm, phase, snapshots=snapshots, bars=bars,
                identities=identities, evidence=reviewed, model=model["model"],
                bias=model["calibration"]["bias"], input_ids=ids)
                for scenario,arm,phase in path_grid(spec)]
            complete = sum(p["accounting_complete"] for p in paths)
            result.update(state="account_replay_complete_metrics_pending" if complete == len(paths) else "account_replay_incomplete", paths=paths,
                paths_executed=len(paths), backtest_executed=True,
                accounting_complete_paths=complete,
                adoption="not_evaluable_statistical_and_attribution_gates_pending")
    result["output_sha256_basis"] = "hash_of_saved_artifact_reported_separately"
    sha = audit.save(output, result)
    print(json.dumps({k:result[k] for k in ("state", "phases", "paths_executed", "adoption")}, ensure_ascii=False))
    print("sha256", sha)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--readiness-sha256", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    run(args.output, args.readiness, args.readiness_sha256, args.evidence, args.model)
