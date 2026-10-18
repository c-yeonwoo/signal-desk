"""Pinned v2 source audit and gated, one-time fit. No production connections."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import gzip
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest, plan, price_inventory, read_bundle
from scripts.measure.kr_net_alpha_readiness import action_sources
from signal_desk.signals import research_net_alpha as core, research_net_alpha_v2 as v2
from signal_desk.signals.research_action_coverage import SourceWindow, CoverageCertificate, OfficialKindScope

COMMIT = "c135516c7e879a94c1507f1cfe5426ca71584928"
ROSTER_SHA = "98060cf7f0ee3a881cf27680a4147941dafacdeeacd28cb0aa33c6e840209ccb"
JOURNAL_SHA = "0a3d872e4598b0171e873a007d1a47640e4ab7ea4fe93b26495d3d44cffca869"
AMENDMENT = "docs/kr-net-alpha-tradability-2026-10-18.json"
PINS = {AMENDMENT: "08e5c689178849b19c5fd8e96b5047287e27f92b881d29e57bcf1c1d1fd996fe",
    "docs/KR-NET-ALPHA-TRADABILITY-2026-10-18.md": "a2ef1cd62648caef3afc5895343893027aabf9fcea7ffcfa09e4b985b5ae2fc1"}


def contract():
    parent = plan()
    for name, sha in PINS.items():
        raw = (ROOT/name).read_bytes()
        committed = subprocess.check_output(["git", "show", f"{COMMIT}:{name}"], cwd=ROOT)
        if digest(raw) != sha or digest(committed) != sha:
            raise ValueError("precommitted v2 amendment differs")
    amendment = json.loads((ROOT/AMENDMENT).read_text())
    if amendment["parent_candidate_id"] != parent["candidate_id"]:
        raise ValueError("v2 parent candidate differs")
    return {**parent, "candidate_id": amendment["candidate_id"], "amendment": amendment,
        "contract_hashes": {**PINS, amendment["parent_document"]: amendment["parent_document_sha256"],
                            amendment["parent_manifest"]: amendment["parent_manifest_sha256"]}}


def source_storage_bytes():
    return (sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
            + sum(p.stat().st_size for p in (ROOT/"data/research").glob("kr-net-alpha-*.zip")))


def price_request_inventory(directory=DEST):
    """Count supplementary raw-price probes without rewriting the pinned ledger."""
    original = json.loads((directory/"requests.json").read_bytes())
    extra_path = directory/"v2-price-requests.json"
    raw = extra_path.read_bytes() if extra_path.exists() else b"[]"
    extra = json.loads(raw)
    if not isinstance(original, list) or not isinstance(extra, list):
        raise ValueError("price request journal malformed")
    return {"price_metadata_attempts": len(original)+len(extra),
            "original_price_metadata_attempts": len(original),
            "supplementary_price_metadata_attempts": len(extra),
            "supplementary_price_journal_sha256": digest(raw)}


def evidence(path, base=ROOT/"data/research"):
    """Load a source-audited packet. Nothing creates complete scopes implicitly."""
    if path is None:
        return v2.Evidence()
    body = json.loads(Path(path).read_text())
    if body.get("schema") != "net-alpha-reviewed-evidence-v1":
        raise ValueError("not a reviewed v2 source packet")
    documents = {}
    base = base.resolve()
    for row in body["documents"]:
        original = (base/row["path"]).resolve()
        if not original.is_relative_to(base) or original.stat().st_size > 100_000_000:
            raise ValueError("evidence path or size outside local research scope")
        if row.get("member"):
            if digest(original.read_bytes()) != row["archive_sha256"]:
                raise ValueError("evidence archive differs")
            with ZipFile(original) as archive:
                if archive.getinfo(row["member"]).file_size > 10_000_000:
                    raise ValueError("oversized original member")
                raw = archive.read(row["member"])
        else:
            raw = original.read_bytes()
        if digest(raw) != row["sha256"] or row["sha256"] in documents:
            raise ValueError("original hash differs or duplicate reference")
        documents[row["sha256"]] = raw
    certs = []
    for row in body.get("certificates", []):
        windows = tuple(SourceWindow(**{**w, "event_kinds": frozenset(w["event_kinds"]),
            "page_hashes": tuple(w["page_hashes"]), "observed_at": v2.timestamp(w["observed_at"])})
            for w in row["windows"])
        # Resolve index references without allowing a second, unbound window.
        scopes = tuple(OfficialKindScope(**{**{k: v for k, v in s.items() if k != "window_index"},
            "window": windows[s["window_index"]],
            "unresolved_event_ids": tuple(s.get("unresolved_event_ids", []))})
            for s in row.get("official_scopes", []))
        certs.append(CoverageCertificate(row["market"], row["security_id"], row["start"], row["end"],
            frozenset(row["required_kinds"]), windows, row.get("reconciliation_sha256"),
            tuple(row.get("unresolved_event_ids", [])), scopes))
    return v2.Evidence(documents, certs, body.get("facts", []))


def identities(days, directory=DEST):
    """Carry only previously observed official identity, never a later snapshot."""
    snapshots = {}
    journal_raw = (directory/"requests.json").read_bytes()
    if digest(journal_raw) != JOURNAL_SHA:
        raise ValueError("pinned identity request journal differs; new sources need a new inventory version")
    journal = json.loads(journal_raw)
    for path in sorted((directory/"stk_isu_base_info").glob("*.zip")):
        manifest, files = read_bundle(path)
        if manifest["endpoint"] != "sto/stk_isu_base_info":
            raise ValueError("wrong historical identity source")
        if len(files) != 1 or not any(r.get("endpoint") == "stk_isu_base_info"
                and r.get("session") == manifest["start"]
                and r.get("sha256") == digest(next(iter(files.values()))) for r in journal):
            raise ValueError("historical identity differs from reserved source response")
        snapshots[manifest["start"]] = {r["ISU_SRT_CD"]: r
            for r in json.loads(next(iter(files.values())))["OutBlock_1"]}
    result, listing, current = {}, {}, {}
    for day in days:
        if day in snapshots:
            current = snapshots[day]
        result[day] = {}
        for ticker in days[day]:
            row = current.get(ticker)
            if row and row["KIND_STKCERT_TP_NM"] == "보통주":
                listed = dt.datetime.strptime(row["LIST_DD"], "%Y%m%d").date().isoformat()
                if listed <= day:
                    result[day][ticker] = row["ISU_CD"]
                    listing[day, ticker] = listed
    return result, listing


def save(path, payload):
    path = Path(path)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        default=lambda v: sorted(v) if isinstance(v, (set, frozenset)) else v.isoformat()).encode()
    if path.suffix == ".gz":
        encoded = gzip.compress(encoded, mtime=0)
    if path.exists() and path.read_bytes() != encoded:
        raise ValueError("artifact differs; use a new output path")
    if not path.exists():
        path.write_bytes(encoded)
    return digest(path.read_bytes())


def run(output, packet=None, fit_output=None):
    spec = contract()
    reviewed = evidence(packet)
    days, sources, missing = price_inventory()
    if missing:
        raise ValueError("fixed source calendar incomplete")
    days = dict(sorted(days.items()))
    dates, index = list(days), {d: i for i, d in enumerate(days)}
    original = (DEST/"universes.json").read_bytes()
    if digest(original) != ROSTER_SHA:
        raise ValueError("pinned original roster differs")
    roster = json.loads(original)
    if (roster["plan_sha256"] != spec["amendment"]["parent_manifest_sha256"]
            or sources != roster["price_sources"]):
        raise ValueError("roster parent provenance differs")
    identity, listing = identities(days)
    partial_sources, _ = action_sources()
    action_journal_path = DEST/"v2-action-requests.json"
    action_journal_raw = action_journal_path.read_bytes() if action_journal_path.exists() else b"[]"
    action_journal = json.loads(action_journal_raw)
    audit = []
    for item in roster["dates"]:
        day, i = item["date"], index[item["date"]]
        raw = [{**s, "listed_on": listing.get((day, s["ticker"]))} for s in item["securities"]]
        if any(identity[day].get(s["ticker"]) != s["isin"] for s in raw):
            raise ValueError("ranked roster identity differs from as-of snapshot")
        snapshot = v2.universe(day, raw, dates[max(0, i-252):i+1], days, reviewed, spec)
        cutoff = (v2.session_time(spec["train_labels_through"]) if day <= spec["train_end"] else
            v2.session_time(spec["calibration_labels_through"]) if day <= spec["calibration_end"] else
            v2.timestamp(spec["amendment"]["labels"]["evaluation_outcome_fact_cutoff"]))
        names = set(snapshot["provisional_tickers"]) | set(snapshot["tickers"])
        labels = {s["ticker"]: v2.label(day, s["ticker"], s["isin"], dates[i+1:i+7],
            days, identity, reviewed, cutoff) for s in raw if s["ticker"] in names}
        audit.append({"date": day, "snapshot": snapshot, "labels": labels,
                      "sample": v2.sample(snapshot, labels)})
    status = v2.readiness(audit, spec)
    result = {"schema": "kr-net-alpha-v2-readiness-v1", "candidate_id": spec["candidate_id"],
        "contract_commit": COMMIT, "contract_hashes": spec["contract_hashes"],
        "implementation_sha256": {str(Path(p).relative_to(ROOT)): digest(Path(p).read_bytes())
            for p in (__file__, v2.__file__, core.__file__)},
        "roster_sha256": digest(original), "price_sources": sources,
        "identity_journal_sha256": JOURNAL_SHA,
        "evidence_packet_sha256": digest(Path(packet).read_bytes()) if packet else None,
        "reviewed_certificates": len(reviewed.certificates), "reviewed_facts": len(reviewed.facts),
        "source_acquisition": {**price_request_inventory(),
            "action_attempts": spec["amendment"]["source_scope"]["previous_action_requests"]+len(action_journal),
            "action_journal_sha256": digest(action_journal_raw),
            "initial_unpreserved_response_attempts": [i for i, r in enumerate(action_journal, 1)
                if r.get("bytes") and not r.get("response_path")]},
        "partial_source_inventory": partial_sources, **status,
        "input_gap_counts": dict(Counter(reason for r in audit for s in r["snapshot"]["rows"]
            for reason in s["reasons"] if s["state"] == "input_gap")),
        "rows": audit, "fit_executed": False, "backtest_executed": False,
        "adoption": "not_evaluable_data_gate", "live_order_eligible": False,
        "strict_pit_eligible": False, "integrated_progress": 40}
    if status["state"] == "ready_for_fit" and fit_output:
        target = Path(fit_output)
        if target.exists():
            raise ValueError("one-fit artifact already exists; do not refit")
        training = [r["sample"] for r in audit if r["sample"] is not None
                    and spec["train_start"] <= r["date"] <= spec["train_end"]]
        calibration = [r["sample"] for r in audit if r["sample"] is not None
                    and spec["calibration_start"] <= r["date"] <= spec["calibration_end"]]
        model = core.fit(training, spec)
        correction = core.calibrate(calibration, model, spec)
        model_sha = save(target, {"schema": "kr-net-alpha-v2-model-v1", "model": model,
            "calibration": correction, "contract_hashes": spec["contract_hashes"],
            "evidence_packet_sha256": result["evidence_packet_sha256"]})
        result.update(fit_executed=True, model_sha256=model_sha, adoption="not_evaluated",
                      state="fit_complete_backtest_pending")
    sha = save(output, result)
    print(json.dumps({k: result[k] for k in ("state", "candidate_id", "phases",
        "reviewed_certificates", "reviewed_facts", "input_gap_counts", "fit_executed", "backtest_executed")},
        ensure_ascii=False))
    print("sha256", sha)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--fit-output", type=Path)
    args = parser.parse_args()
    run(args.output, args.evidence, args.fit_output)
