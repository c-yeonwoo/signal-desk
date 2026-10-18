"""Fixed Korean listing controls: original identity/counts, not action-free scopes."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re

from scripts.measure.acquire_historical_sources import write_archive
from scripts.measure.contract_event_sources import SEARCH, _Form, _text
from scripts.measure.kr_action_source_audit import Collector, census, original
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest, read_bundle
from scripts.measure.kr_net_alpha_v2 import save, source_storage_bytes
from scripts.measure.kr_net_alpha_fsc_rights import parse_response
from signal_desk import market_clock

CASES = (("035720", "2021-04-15"), ("267250", "2021-04-13"),
         ("017670", "2021-11-29"), ("009830", "2023-03-31"),
         ("003620", "2023-04-28"), ("010060", "2023-05-30"))
POLICY_SHA = "5e693bc714774000ca2c2e458d2930aa0737114a8e94a70cae2d154da346903b"


def raw_split_transition(fields):
    """Corroborate actual KRX count transition, not the earlier proposed counts."""
    day, ticker = fields["listed_on"], fields["ticker"]
    folder = DEST/"stk_bydd_trd"
    previous_day = market_clock.previous_session("kr", day)
    if previous_day is None or not market_clock.is_session("kr", day):
        raise ValueError("split observation market calendar unavailable")
    previous = folder/(previous_day+".zip")
    points = []
    for path in (previous, folder/(day+".zip")):
        manifest, members = read_bundle(path)
        raw = next(iter(members.values()))
        rows = [r for r in json.loads(raw)["OutBlock_1"] if r["ISU_CD"] == ticker]
        if len(rows) != 1 or rows[0]["BAS_DD"] != path.stem.replace("-", ""):
            raise ValueError("raw split observation identity/date differs")
        points.append({"on": path.stem, "shares": int(rows[0]["LIST_SHRS"].replace(",", "")),
            "source_path": str(path.relative_to(ROOT)), "archive_sha256": digest(path.read_bytes()),
            "source_sha256": digest(raw)})
    matches = [p["shares"] for p in points] == [fields["listed_shares_before"], fields["listed_shares_after"]]
    return {"observations": points, "matches_original": matches, "economic_scope_certified": False}


def identity(ticker, day):
    paths = [p for p in sorted((DEST/"stk_isu_base_info").glob("*.zip")) if p.stem < day]
    if not paths:
        raise ValueError("prior official security snapshot missing")
    p = paths[-1]
    manifest, members = read_bundle(p)
    name, raw = next(iter(members.items()))
    rows = [r for r in json.loads(raw)["OutBlock_1"] if r["ISU_SRT_CD"] == ticker
            and r["KIND_STKCERT_TP_NM"] == "보통주"]
    if len(rows) != 1:
        raise ValueError("official ordinary identity ambiguous")
    return {"ticker": ticker, "security_id": rows[0]["ISU_CD"], "share_class": "ordinary",
        "snapshot_on": manifest["start"], "source_path": str(p.relative_to(ROOT)),
        "archive_sha256": digest(p.read_bytes()), "member": name, "sha256": digest(raw)}


def listing_fields(raw, binding):
    """Keep observations separate; only exact simple-split arithmetic is resolved."""
    text = _text(raw.decode())
    pairs = re.findall(r"(KR[0-9A-Z]{10})\s*\(단축코드:A([0-9]{6})\)", text)
    wanted = (binding["security_id"], binding["ticker"])
    if wanted not in pairs or "보통주" not in text:
        raise ValueError("listing ordinary-share identity differs")
    def dates(part):
        return [dt.date(*map(int, m)).isoformat() for m in
            re.findall(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", part)]
    result = {"security_id": wanted[0], "ticker": wanted[1], "share_class": "ordinary",
        "source_sha256": digest(raw), "source_span": text,
        "identity_bound": True, "economic_scope_certified": False,
        "holder_quantity_rule_verified": False}
    ordinary_counts = re.findall(r"기명식 보통주 ([\d,]+)주\s*(?:\([^)]*\)\s*)?→\s*([\d,]+)주", text)
    if len(ordinary_counts) == 1:
        result["observed_ordinary_counts"] = [int(v.replace(",", "")) for v in ordinary_counts[0]]
    listing_dates = re.findall(r"변경상장일\s*:\s*(\d{4}년\s*\d{1,2}월\s*\d{1,2}일)", text)
    if len(listing_dates) == 1:
        result["observed_listing_on"] = dates(listing_dates[0])[0]
    # A complex/multi-class listing must not inherit a simple split's ratio.
    if "변경사유 : 액면분할" not in text or "우선주" in text or set(pairs) != {wanted}:
        return {**result, "state": "identified_original_requires_event_specific_review"}
    counts = re.findall(r"기명식 보통주 ([\d,]+)주\s*\([^)]*\)\s*→\s*([\d,]+)주", text)
    faces = re.findall(r"1주의 금액\s*:\s*([\d,]+)원\s*→\s*([\d,]+)원", text)
    deltas = re.findall(r"변경주식수\s*:\s*([\d,]+)주", text)
    if len(counts) != 1 or len(faces) != 1 or len(deltas) != 1:
        raise ValueError("simple split amounts ambiguous")
    before, after = [int(v.replace(",", "")) for v in counts[0]]
    old_face, new_face = [int(v.replace(",", "")) for v in faces[0]]
    if min(before, after, old_face, new_face) <= 0 or before*old_face != after*new_face or int(deltas[0].replace(",", "")) != after-before:
        raise ValueError("simple split arithmetic differs")
    issued = dates(text.split("② 발행일", 1)[1].split("③", 1)[0])
    listed = dates(text.split("⑤ 변경상장일 :", 1)[1].split("⑥", 1)[0])
    if len(issued) != 1 or len(listed) != 1 or issued[0] > listed[0]:
        raise ValueError("simple split issue/listing dates ambiguous")
    return {**result, "state": "simple_split_terms_reviewed", "issued_on": issued[0],
        "listed_on": listed[0], "listed_shares_before": before, "listed_shares_after": after,
        "old_face_value": old_face, "new_face_value": new_face,
        "share_ratio": old_face/new_face, "holder_quantity_rule_verified": True}


def collect(output, report_path, *, recheck_issuance=False):
    output, report_path = Path(output).resolve(), Path(report_path).resolve()
    if any(not p.is_relative_to((ROOT/"data/research").resolve()) or p.exists() for p in (output, report_path)):
        raise ValueError("listing audit requires new local research outputs")
    collector = Collector()
    initial = len(collector.journal)
    files, cases = {}, []
    policy = (DEST/"effective-source-responses/0152.bin").read_bytes()
    if digest(policy) != POLICY_SHA:
        raise ValueError("official issuance policy differs")
    spec = json.loads(re.search(r"const swaggerJson = `(.*?)`;", policy.decode(), re.S)[1])
    endpoint = "https://"+spec["host"]+"/getStocIssuInfo_V3"
    from scripts.measure.kr_action_source_audit import FSC_ISSUANCE
    if endpoint != FSC_ISSUANCE or "/getStocIssuInfo_V3" not in spec["paths"]:
        raise ValueError("official issuance endpoint differs")
    files["policy/issuance-api.html"] = policy
    # A provider rejection is archived once, not retried for every company.
    raw, meta = collector.fsc_issuance("2021-04-14", issuer="카카오", recheck=recheck_issuance)
    files["raw/issuance-probe.json"] = raw
    api = {**parse_response(raw, "2021-04-14"), "http_status": meta.get("http_status"),
        "source_sha256": meta["sha256"], "endpoint": endpoint}
    files["raw/issuance-metadata.json"] = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode()
    form_raw, form_meta = collector.get(SEARCH+"?method=searchDetailsMain")
    files["raw/search-form.html"] = form_raw
    form = _Form(); form.feed(form_raw.decode())

    def preserve(meta):
        p = (DEST/meta["response_path"]).resolve()
        if not p.is_relative_to(DEST.resolve()) or digest(p.read_bytes()) != meta["sha256"]:
            raise ValueError("listing source bytes differ")
        files["responses/"+meta["response_path"]] = p.read_bytes()

    for ticker, day in CASES:
        start = (dt.date.fromisoformat(day)-dt.timedelta(days=14)).isoformat()
        term = {"017670": "재상장", "003620": "매매거래정지"}.get(ticker, "변경상장")
        listing = census(collector, form.fields, start, day, max_pages=3, term=term)
        for page in listing["pages"]:
            preserve(page)
        selected = [r for r in listing["rows"] if r["company_code"] == ticker[:5]]
        case = {"ticker": ticker, "target_session": day, "query": listing,
            "selected_receipts": [r["receipt"] for r in selected], "originals": [],
            "selection_basis": "company_code_candidates_not_historical_security_census",
            "empty_query_means_no_actions": False}
        if len(selected) > 6:
            raise ValueError("fixed listing control exceeds original cap")
        for row in selected:
            review = {"listing": row}
            try:
                raw, provenance = original(collector, row["receipt"])
                review["provenance"] = provenance
                for k in ("viewer", "resolver", "original"):
                    preserve(provenance[k])
                binding = identity(ticker, row["published_at"][:10])
                review["identity"] = binding
                review["fields"] = listing_fields(raw, binding)
                if review["fields"].get("listed_on") not in (None, day):
                    raise ValueError("split listing date differs from target")
                if review["fields"]["state"] == "simple_split_terms_reviewed":
                    review["raw_transition"] = raw_split_transition(review["fields"])
            except ValueError as exc:
                review.update(state="source_unresolved", reason=str(exc))
            case["originals"].append(review)
        cases.append(case)
    for meta in collector.journal[initial:]:
        if meta.get("response_path"):
            preserve(meta)
    report = {"schema": "kr-net-alpha-listing-crosscheck-v1", "candidate_id": collector.spec["candidate_id"],
        "contract_hashes": collector.spec["contract_hashes"], "issuance_probe": api, "cases": cases,
        "new_requests": len(collector.journal)-initial,
        "cumulative_action_attempts": 7+len(collector.journal), "source_scope_certified": False,
        "fit_executed": False, "backtest_executed": False, "adoption": "not_evaluable_data_gate",
        "integrated_progress": 40}
    files["crosscheck.json"] = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    files["implementation/audit.py"] = Path(__file__).read_bytes()
    files["implementation/collector.py"] = (ROOT/"scripts/measure/kr_action_source_audit.py").read_bytes()
    files["implementation/parser.py"] = (ROOT/"scripts/measure/kr_net_alpha_fsc_rights.py").read_bytes()
    # Include cached requests too: their original observation time is not now.
    files["journals/action-requests.json"] = json.dumps(collector.journal, ensure_ascii=False, sort_keys=True).encode()
    if source_storage_bytes()+sum(map(len, files.values())) > collector.spec["max_new_archive_bytes"]:
        raise ValueError("listing archive exceeds cumulative byte cap")
    write_archive(output, files, {"schema": "kr-net-alpha-listing-originals-v1", "entries": {
        n: {"sha256": digest(b), "bytes": len(b)} for n,b in files.items()}, "economic_scope_certified": False})
    report.update(archive=str(output.relative_to(ROOT)), archive_sha256=digest(output.read_bytes()))
    save(report_path, report)
    print(json.dumps({"archive_sha256": report["archive_sha256"], "issuance_state": api["state"],
        "new_requests": report["new_requests"], "cumulative_action_attempts": report["cumulative_action_attempts"],
        "cases": [{"ticker": c["ticker"], "receipts": c["selected_receipts"],
            "states": [o.get("state", o.get("fields", {}).get("state")) for o in c["originals"]]} for c in cases]}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--recheck-issuance", action="store_true",
        help="one explicit new authorization probe; preserve the prior failure")
    args = parser.parse_args()
    collect(args.output, args.report, recheck_issuance=args.recheck_issuance)
