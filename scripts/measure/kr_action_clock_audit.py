"""Replay the frozen action controls and audit actual purpose-specific scopes."""

import argparse
from collections import Counter, defaultdict
import datetime as dt
from functools import lru_cache
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile

from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest, read_bundle
from scripts.measure.kr_net_alpha_v2 import contract, evidence, save, run
from scripts.measure.contract_event_sources import _text
from signal_desk.signals import research_action_clocks as clocks, research_net_alpha_v2 as v2

ARCHIVE = "data/research/kr-net-alpha-effective-source-audit-2026-10-18-v1.zip"
ARCHIVE_SHA = "08b62917dd0d4c6998ca6b1cc9776bfdd09f7c2e8bc2d26616231711bcf1e83b"


def controls_packet():
    """Selected date facts only. No action-free interval is inferred here."""
    spec = contract()
    path = ROOT/ARCHIVE
    if digest(path.read_bytes()) != ARCHIVE_SHA:
        raise ValueError("frozen action control archive differs")
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        files = {n: archive.read(n) for n in manifest["entries"]}
        if any(digest(raw) != manifest["entries"][n]["sha256"] for n, raw in files.items()):
            raise ValueError("frozen control member differs")
    by_hash = {digest(raw): (name, raw) for name, raw in files.items()}
    documents, assertions, bindings = {}, [], {}

    def reference(sha):
        name, raw = by_hash[sha]
        documents.setdefault(sha, {"path": str(path.relative_to(ROOT/"data/research")),
            "archive_sha256": ARCHIVE_SHA, "member": name, "sha256": sha})
        return raw

    def identity(ticker, published):
        if ticker in bindings:
            return bindings[ticker]["security_id"]
        paths = [p for p in sorted((DEST/"stk_isu_base_info").glob("*.zip")) if p.stem < published]
        if not paths:
            raise ValueError("no prior official identity snapshot")
        p = paths[-1]
        source, members = read_bundle(p)
        name, raw = next(iter(members.items()))
        rows = [r for r in json.loads(raw)["OutBlock_1"] if r["ISU_SRT_CD"] == ticker
                and r["KIND_STKCERT_TP_NM"] == "보통주"]
        if len(rows) != 1:
            raise ValueError("ambiguous official ordinary share binding")
        sha = digest(raw)
        documents[sha] = {"path": str(p.relative_to(ROOT/"data/research")), "archive_sha256": digest(p.read_bytes()),
                          "member": name, "sha256": sha}
        bindings[ticker] = {"ticker": ticker, "security_id": rows[0]["ISU_CD"], "share_class": "ordinary",
            "snapshot_on": source["start"], "source_sha256": sha}
        return rows[0]["ISU_CD"]

    def add(receipt, ticker, event, kind, component, fields, *, previous=(), status="confirmed", report=None):
        report = report or json.loads(files[f"observations/kr-net-alpha-kind-effective-{receipt}-v1.json"])
        original_sha = report["original"]["sha256"]
        reference(original_sha)
        pub = report["listing"]["published_at"]
        token = pub.replace("T", " ")[:16]
        choices = []
        for name, raw in files.items():
            if name.startswith("responses/") and receipt.encode() in raw and token[:10].encode() in raw:
                for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", raw.decode("utf-8"), re.S):
                    span = _text(tr)
                    if receipt in tr and token in span:
                        choices.append((name, digest(raw), span))
        if not choices:
            raise ValueError("receipt-bound publication row missing")
        _, pubsha, span = sorted(choices)[0]
        reference(pubsha)
        assertion = {"assertion_id": receipt+":"+component, "event_id": event,
            "component_id": component, "component_kind": component, "action_kind": kind,
            "security_id": identity(ticker, pub[:10]), "share_class": "ordinary",
            "status": status, "supersedes_assertion_ids": list(previous),
            "availability_mode": "reconstructed_public", "observed_at": report["original"]["observed_at"],
            "published_on": pub[:10], "published_at": pub, "publication_precision": "minute",
            "publication_proof": {"source_sha256": pubsha, "source_span": span}, "changes": {}}
        for name, (value, phrase, precision) in fields.items():
            assertion["changes"][name] = {"value": value, "source_span": phrase,
                "source_sha256": original_sha, "precision": precision}
        assertions.append(assertion)
        return assertion

    add("20210225000978", "035720", "kakao-split-2021", "split", "schedule", {
        "legal_effective_on": ("2021-04-14", "신주의 효력발생일 2021-04-14", "date"),
        "share_ratio": (5, "500 100", "value"),
        "tradable_from_session": ("2021-04-15", "신주권상장예정일 2021-04-15", "date")}, status="announced")
    add("20210414000560", "035720", "kakao-split-2021", "split", "price_unit", {
        "price_basis_from_session": ("2021-04-15", "적용일 2021-04-15", "date")})
    # The exchange confirms the price date; the ratio's source remains the decision.
    # Do not promote announced absolute counts or legal completion on this basis.
    add("20210204000385", "267250", "hyundai-split-2021", "split", "schedule", {
        "legal_effective_on": ("2021-04-09", "신주의 효력발생일 2021-04-09", "date"),
        "tradable_from_session": ("2021-04-12", "신주권상장예정일 2021-04-12", "date"),
        "share_ratio": (5, "5,000 1,000", "value")}, status="announced")
    add("20210223000281", "267250", "hyundai-split-2021", "split", "schedule", {
        "legal_effective_on": ("2021-04-12", "신주의 효력발생일 2021-04-12", "date"),
        "tradable_from_session": ("2021-04-13", "신주권상장예정일 2021-04-13", "date")},
        previous=("20210204000385:schedule",), status="announced")
    add("20210412000696", "267250", "hyundai-split-2021", "split", "price_unit", {
        "price_basis_from_session": ("2021-04-13", "적용일 2021-04-13", "date")})
    add("20221014000327", "003620", "ssangyong-price-rule", "capital_reduction", "quote_rule", {
        "maximum_quote": (13150, "8,760 13,150 4,380", "value"),
        "quote_rule_from_session": ("2022-10-17", "적용일 2022-10-17", "date"),
        "trading_halt_continues": (True, "매매거래정지가 계속됨", "value")})
    add("20230125000390", "003620", "ssangyong-price-rule", "capital_reduction", "quote_rule", {
        "maximum_quote": (13140, "8,760 13,140 4,380", "value"),
        "quote_rule_from_session": ("2023-01-26", "적용일 2023-01-26", "date")},
        previous=("20221014000327:quote_rule",))
    # POSCO has a preserved original but not one of the 31 observation objects.
    sha = "fb2536b3d7b100cd9f41ac21afdc11ffc94c19004569a20265b555281b0a5fd6"
    raw = reference(sha)
    controls = {"posco": {"source_sha256": sha, "explicit_ex_session": "2021-03-30",
        "original_has_date": "2021-03-30" in _text(raw.decode())},
        "kakao": {"announced_ratio": 5, "price_session": "2021-04-15", "economic_scope_certified": False}}
    row = next(r for n, content in files.items() if n.startswith("observations/kr-net-alpha-rights-census-")
        for r in json.loads(content)["rows"] if r["receipt"] == "20210329001215")
    attempts = json.loads(files["journals/v2-action-requests.json"])
    original_meta = next(r for r in attempts if r.get("sha256") == sha)
    add("20210329001215", "005490", "posco-q1-dividend-2021", "cash_dividend", "entitlement", {
        "entitlement_boundary": ({"on": "2021-03-30", "phase": "before_open"}, "2021-03-30", "session_phase")},
        report={"original": original_meta, "listing": row})
    samsung_sha = "00bda6a7b3ab830d11c9245466604df1aff9334afca3acc464a03d79e87601bc"
    raw = reference(samsung_sha)
    text = _text(raw.decode())
    if "Quarterly Dividend for First Quarter 2021 Apr 29. 2021" not in text:
        raise ValueError("Samsung dated publication heading differs")
    assertions.append({"assertion_id": "samsung-q1-2021:entitlement", "event_id": "samsung-q1-2021",
        "component_id": "entitlement", "component_kind": "entitlement", "action_kind": "cash_dividend",
        "security_id": identity("005930", "2021-04-29"), "share_class": "ordinary", "status": "confirmed",
        "supersedes_assertion_ids": [], "published_on": "2021-04-29", "publication_precision": "date",
        "availability_mode": "reconstructed_public", "observed_at": next(r["observed_at"] for r in attempts if r.get("sha256") == samsung_sha),
        "publication_proof": {"source_sha256": samsung_sha,
            "source_span": "Quarterly Dividend for First Quarter 2021 Apr 29. 2021", "date_format": "english_abbrev_dot"},
        "changes": {name: {"value": val, "precision": precision, "source_sha256": samsung_sha, "source_span": phrase}
            for name, val, precision, phrase in [
                ("record_on", "2021-03-31", "date", "Record date: March 31, 2021"),
                ("cash_per_share", 361, "value", "Common shares: KRW 361"),
                ("currency", "KRW", "value", "Common shares: KRW 361"),
                ("amount_status", "final", "value", "approved a dividend plan"),
                ("payment_scheduled_on", "2021-05-18", "date", "Scheduled payment date: May 18, 2021")]}})
    controls["samsung"] = {"record_on": "2021-03-31", "cash_per_share": 361,
        "entitlement_boundary": None, "payment_confirmed_on": None, "label_ready": False}
    # Keep the previously validated halt instead of inventing a complete scope.
    prior = ROOT/"data/research/kr-net-alpha-v2-asiana-evidence-2026-10-18-v1.json"
    old = json.loads(prior.read_text())
    for doc in old["documents"]:
        documents.setdefault(doc["sha256"], doc)
    packet = {"schema": "net-alpha-action-components-v1", "documents": list(documents.values()),
        "assertions": assertions, "facts": old["facts"], "certificates": old["certificates"],
        "identity_bindings": list(bindings.values()), "source_archive_sha256": ARCHIVE_SHA,
        "contract_hashes": spec["contract_hashes"], "scope_promotion": False}
    return packet, controls, files


def purpose_scopes(audit, spec):
    """Union the actual U's input/label dependencies, never an entire archive."""
    windows = defaultdict(list)
    for row in audit["rows"]:
        day = row["date"]
        phase = "train" if day <= spec["train_end"] else "calibration" if day <= spec["calibration_end"] else "evaluation"
        history = market_sessions_before(day, spec["required_closes"])
        needed = set(row["snapshot"]["provisional_tickers"]) | set(row["snapshot"]["tickers"])
        for stock in row["snapshot"]["rows"]:
            if stock["ticker"] not in needed:
                continue
            key = (phase, stock["security_id"], stock["ticker"])
            windows[key+("input",)].append((history[0], day))
            sessions = v2.label_sessions(day)
            windows[key+("outcome",)].append((sessions[0], sessions[-1]))
    scopes = []
    for (phase, isin, ticker, purpose), spans in sorted(windows.items()):
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        kinds = v2.UNIT_KINDS if purpose == "input" else v2.LABEL_KINDS
        for start, end in merged:
            scopes.append({"phase": phase, "security_id": isin, "ticker": ticker, "purpose": purpose,
                "start": start, "end": end, "required_kinds": sorted(kinds)})
    return scopes


@lru_cache(maxsize=1024)
def market_sessions_before(day, count):
    from signal_desk import market_clock
    return [s.date().isoformat() for s in market_clock._calendar("kr").sessions_in_range(
        (dt.date.fromisoformat(day)-dt.timedelta(days=500)).isoformat(), day)][-count:]


def execute(packet_path, readiness_path, report_path, fit_output=None):
    packet, controls, files = controls_packet()
    save(packet_path, packet)
    reviewed = evidence(packet_path)
    spec = contract()
    # Actual raw KRX values, with the old-unit reference close left in the source.
    closes, volumes = [], []
    for day in ("2021-04-14", "2021-04-15"):
        with ZipFile(io.BytesIO(files[f"prior/prices-{day}.zip"])) as z:
            row = next(r for r in json.loads(z.read(f"raw/{day}.json"))["OutBlock_1"] if r["ISU_CD"] == "035720")
            closes.append(float(row["TDD_CLSPRC"])); volumes.append(float(row["ACC_TRDVOL"]))
    controls["kakao"]["raw_closes"] = closes
    controls["kakao"]["raw_volumes"] = volumes
    controls["kakao"]["unit_only_reference_close"] = closes[0]/5
    controls["kakao"]["unit_only_price_delta"] = closes[1]/(closes[0]/5)-1
    for ticker, early, late in [("267250", "2021-02-22", "2021-02-24"), ("003620", "2023-01-20", "2023-01-26")]:
        isin = next(b["security_id"] for b in packet["identity_bindings"] if b["ticker"] == ticker)
        controls[ticker] = {"before": reviewed.actions.resolve_components(isin, clocks.session_time(early), "account"),
            "after": reviewed.actions.resolve_components(isin, clocks.session_time(late), "account")}
    result = run(readiness_path, packet_path, fit_output)
    scopes = purpose_scopes(result, spec)
    for scope in scopes:
        cert = reviewed.coverage(scope["security_id"], scope["start"], scope["end"], frozenset(scope["required_kinds"]))
        scope["state"] = "usable" if cert else "source_unavailable"
        scope["reason"] = None if cert else "effective_period_kind_and_correction_scope_unverified"
    census = [json.loads(raw) for n, raw in files.items() if n.startswith("observations/kr-net-alpha-rights-census-")]
    policy_path = ROOT/"data/research/kr-net-alpha-action-source-policy-2026-10-18-v1.zip"
    policy = None
    if policy_path.exists():
        with ZipFile(policy_path) as archive:
            policy = json.loads(archive.read("policy-review.json"))
        policy = {"archive": str(policy_path.relative_to(ROOT)), "sha256": digest(policy_path.read_bytes()),
                  "observation": policy}
    report = {"schema": "kr-net-alpha-action-clock-scope-audit-v1", "contract_hashes": spec["contract_hashes"],
        "implementation_sha256": digest(Path(__file__).read_bytes()), "source_policy_observation": policy,
        "packet_sha256": digest(Path(packet_path).read_bytes()), "readiness_sha256": digest(Path(readiness_path).read_bytes()),
        "source_archive_sha256": ARCHIVE_SHA, "controls": controls, "scopes": scopes,
        "scope_states": dict(Counter(s["state"] for s in scopes)), "scope_kinds": dict(Counter(k for s in scopes for k in s["required_kinds"])),
        "preserved_listing": {"rows": sum(len(c["rows"]) for c in census), "query_basis": "publication",
            "all_kinds_certified": False, "corrections_complete": False},
        "phases": result["phases"], "state": result["state"], "fit_executed": result["fit_executed"],
        "backtest_executed": result["backtest_executed"], "adoption": result["adoption"], "integrated_progress": 40}
    sha = save(report_path, report)
    print(json.dumps({"report_sha256": sha, "assertions": len(reviewed.actions.assertions),
        "scope_states": report["scope_states"], "state": report["state"]}))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fit-output", type=Path)
    args = parser.parse_args()
    execute(args.packet, args.readiness, args.report, args.fit_output)
