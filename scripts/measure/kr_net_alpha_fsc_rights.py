"""Bounded FSC historical schedule probes, not economic scope certificates."""

import argparse
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from scripts.measure.acquire_historical_sources import write_archive
from scripts.measure.kr_action_source_audit import Collector, FSC_RIGHTS
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest
from scripts.measure.kr_net_alpha_v2 import save, source_storage_bytes

POLICY = DEST/"effective-source-responses/0151.bin"
POLICY_SHA = "c9fa40211854ce48191cd75cb6210e86d2040a50ba0a5bee2ac56d117d0a4be1"
PROBES = (("2023-12-28", "삼성전자", 1), ("2021-04-15", "카카오", 1),
          ("2021-03-31", "삼성전자", 100), ("2021-04-15", None, 100))


def official_operation():
    raw = POLICY.read_bytes()
    if digest(raw) != POLICY_SHA:
        raise ValueError("preserved FSC specification differs")
    match = re.search(r"const swaggerJson = `(.*?)`;", raw.decode(), re.S)
    spec = json.loads(match[1])
    if "https://"+spec["host"]+"/getRighExerReasSche_V2" != FSC_RIGHTS:
        raise ValueError("official FSC operation differs")
    params = spec["paths"]["/getRighExerReasSche_V2"]["parameters"]
    if {p["name"] for p in params if p.get("required")} != {"serviceKey"}:
        raise ValueError("FSC authentication specification differs")
    return raw


def parse_response(raw, day, page=1, page_size=100):
    """Nonzero totals, HTTP 200 or valid authentication alone certify no scope."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        try:
            root = ET.fromstring(raw)
            code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode")
        except ET.ParseError:
            code = None
        return {"state": "api_rejected" if code and code not in ("00", "0") else "unrecognized_response",
                "result_code": code, "rows": [], "total_count": None}
    if not isinstance(body, dict):
        raise ValueError("FSC JSON root is not an object")
    gateway = body.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader", {})
    if gateway.get("returnReasonCode"):
        return {"state": "api_rejected", "result_code": str(gateway["returnReasonCode"]),
                "rows": [], "total_count": None}
    response = body.get("response", body)
    if not isinstance(response, dict):
        raise ValueError("FSC response is not an object")
    header = response.get("header", {})
    code = str(header.get("resultCode", ""))
    if code not in ("00", "0"):
        return {"state": "api_rejected" if code else "unrecognized_response",
                "result_code": code or None, "rows": [], "total_count": None}
    result = response.get("body", {})
    count = int(result["totalCount"])
    if count < 0 or int(result["pageNo"]) != page or int(result["numOfRows"]) != page_size:
        raise ValueError("FSC count or requested page differs")
    items = result.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("FSC items malformed")
    if len(rows) > page_size or len(rows) > count or (count == 0 and rows):
        raise ValueError("FSC row count differs")
    if any(str(r.get("basDt")) != day.replace("-", "") for r in rows):
        raise ValueError("FSC basis date mismatch")
    if count and not rows and (page-1)*page_size < count:
        raise ValueError("FSC page unexpectedly empty")
    return {"state": "page_received" if rows else "empty_not_action_free", "result_code": code,
        "rows": rows, "total_count": count, "page_complete": len(rows) == min(page_size, max(0,count-(page-1)*page_size)),
        "all_pages_received": count == len(rows) and page == 1,
        "economic_scope_certified": False}


def schedule_pages(collector, day, issuer=None, max_pages=3):
    """Complete a bounded record-date query, without calling it an event census."""
    if type(max_pages) is not int or not 1 <= max_pages <= 30:
        raise ValueError("FSC page cap outside bounded query")
    rows, pages, total = [], [], None
    for page in range(1, max_pages+1):
        raw, meta = collector.fsc_rights(day, page=page, issuer=issuer)
        result = parse_response(raw, day, page=page)
        if meta.get("http_status") != 200 or result["state"] not in ("page_received", "empty_not_action_free"):
            raise ValueError("FSC schedule page not available")
        if not result["page_complete"]:
            raise ValueError("FSC schedule page incomplete")
        if total is not None and total != result["total_count"]:
            raise ValueError("FSC schedule count changed between pages")
        total = result["total_count"]
        rows.extend(result["rows"])
        pages.append({"raw": raw, "meta": meta})
        if len(rows) >= total:
            break
    keys = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows]
    return {"basis_on": day, "issuer_filter": issuer, "rows": rows,
        "total_count": total, "complete_query": len(rows) == total and len(set(keys)) == len(keys),
        "duplicate_rows": len(keys)-len(set(keys)), "economic_scope_certified": False}, pages


def issuer_roles(rows, name, issuer_code):
    """An issuer match is not a common-share/ISIN match or an economic event."""
    selected = [r for r in rows if r.get("stckIssuCmpyNm") == name and r.get("scrsIssuMnbdCd") == issuer_code]
    grouped = {}
    for row in selected:
        key = (row["stckIssuRcd"], row["rgtExertRcd"])
        role = grouped.setdefault(key, {"issuance_reason": row["stckIssuRcdNm"],
            "schedule_role": row["rgtExertRcdNm"], "observations": []})
        point = {"start_on": row["rgtExertSttgDt"], "end_on": row["rgtExertEdDt"], "face_value": row["stckParPrc"]}
        if point not in role["observations"]:
            role["observations"].append(point)
    return {"issuer": name, "issuer_code": issuer_code, "selected_rows": len(selected),
        "excluded_issuers": sorted({r.get("stckIssuCmpyNm", "") for r in rows if r not in selected}),
        "roles": [grouped[k] for k in sorted(grouped)], "share_class_verified": False,
        "payment_completed_verified": False, "economic_scope_certified": False}


def inspect_crosscheck(path, output):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT/"data/research").resolve()) or output.exists():
        raise ValueError("FSC review requires a new local research output")
    source = json.loads(Path(path).read_text())
    archive_path = (ROOT/source["archive"]).resolve()
    if not archive_path.is_relative_to((ROOT/"data/research").resolve()) or digest(archive_path.read_bytes()) != source["archive_sha256"]:
        raise ValueError("FSC crosscheck archive differs")
    from zipfile import ZipFile
    with ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if any(digest(archive.read(n)) != r["sha256"] for n,r in manifest["entries"].items()):
            raise ValueError("FSC crosscheck original differs")
        preserved = json.loads(archive.read("crosscheck.json"))
        if any(source.get(k) != v for k,v in preserved.items()):
            raise ValueError("FSC crosscheck report differs from preserved original")
    queries = {q["case_id"]: q for q in source["queries"]}
    targets = {"kakao-split": ("카카오", "03572"), "substring-control": ("카카오", "03572"),
        "samsung-dividend": ("삼성전자", "00593"), "hyundai-split": ("현대중공업지주", "26725"),
        "posco-dividend": ("포스코", "00549")}
    result = {"schema": "kr-net-alpha-fsc-schedule-role-review-v1", "source_report_sha256": digest(Path(path).read_bytes()),
        "source_archive_sha256": source["archive_sha256"], "implementation_sha256": digest(Path(__file__).read_bytes()),
        "cases": {case: issuer_roles(queries[case]["rows"], *target) for case,target in targets.items()},
        "scope_basis": "queried_basis_dates_not_full_effective_period_census",
        "historical_publication_revisions_verified": False, "independent_of_kis_ksd_schedule": False,
        "scope_certificate_eligible": False, "fit_executed": False, "backtest_executed": False,
        "adoption": "not_evaluable_data_gate", "integrated_progress": 40}
    sha = save(output, result)
    print(json.dumps({"sha256": sha, "issuer_rows": {k:v["selected_rows"] for k,v in result["cases"].items()}}, ensure_ascii=False))
    return result


def crosscheck(output, report_path):
    output, report_path = Path(output).resolve(), Path(report_path).resolve()
    if any(not p.is_relative_to((ROOT/"data/research").resolve()) for p in (output, report_path)):
        raise ValueError("FSC evidence must stay in local research storage")
    if output.exists() or report_path.exists():
        raise ValueError("refusing to overwrite FSC crosscheck")
    collector = Collector()
    initial = len(collector.journal)
    files, queries = {"policy/rights-api.html": official_operation()}, []
    cases = (("market", "2021-04-15", None), ("substring-control", "2021-04-15", "카카오"),
        ("kakao-split", "2021-04-13", "카카오"), ("samsung-dividend", "2021-03-31", "삼성전자"),
        ("hyundai-split", "2021-04-11", "현대중공업지주"), ("posco-dividend", "2021-03-31", "포스코"))
    for name, day, issuer in cases:
        query, pages = schedule_pages(collector, day, issuer)
        for i, page in enumerate(pages, 1):
            files[f"raw/{name}-{i}.json"] = page["raw"]
        query.update(case_id=name, source_pages=[{k:p["meta"][k] for k in ("response_path", "sha256")} for p in pages])
        queries.append(query)
    report = {"schema": "kr-net-alpha-fsc-rights-crosscheck-v1", "candidate_id": collector.spec["candidate_id"],
        "contract_hashes": collector.spec["contract_hashes"], "endpoint": FSC_RIGHTS,
        "implementation_sha256": digest(Path(__file__).read_bytes()), "queries": queries,
        "new_requests": len(collector.journal)-initial, "cumulative_action_attempts": 7+len(collector.journal),
        "economic_scope_certified": False, "strict_pit_eligible": False, "integrated_progress": 40}
    files["crosscheck.json"] = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    files["implementation/probe.py"] = Path(__file__).read_bytes()
    files["implementation/collector.py"] = (ROOT/"scripts/measure/kr_action_source_audit.py").read_bytes()
    files["journals/requests.json"] = json.dumps(collector.journal[initial:], ensure_ascii=False, sort_keys=True).encode()
    if source_storage_bytes()+sum(map(len, files.values())) > collector.spec["max_new_archive_bytes"]:
        raise ValueError("FSC archive exceeds cumulative source byte cap")
    write_archive(output, files, {"schema": "kr-net-alpha-fsc-rights-originals-v1", "entries": {
        n: {"sha256": digest(raw), "bytes": len(raw)} for n,raw in files.items()}, "economic_scope_certified": False})
    report.update(archive=str(output.relative_to(ROOT)), archive_sha256=digest(output.read_bytes()))
    save(report_path, report)
    print(json.dumps({"archive_sha256": report["archive_sha256"], "new_requests": report["new_requests"],
        "queries": [{k:q[k] for k in ("case_id", "total_count", "complete_query", "duplicate_rows")} for q in queries]}, ensure_ascii=False))
    return report


def execute(output, report_path):
    output, report_path = Path(output).resolve(), Path(report_path).resolve()
    if any(not Path(p).resolve().is_relative_to((ROOT/"data/research").resolve()) for p in (output, report_path)):
        raise ValueError("FSC evidence must stay in local research storage")
    if Path(output).exists() or Path(report_path).exists():
        raise ValueError("refusing to overwrite FSC probe evidence")
    policy = official_operation()
    collector = Collector()
    initial = len(collector.journal)
    files, queries = {"policy/rights-api.html": policy}, []
    for number, (day, issuer, page_size) in enumerate(PROBES, 1):
        query = {"basis_on": day, "issuer_filter": issuer, "page_size": page_size}
        before = len(collector.journal)
        raw, meta = None, None
        try:
            raw, meta = collector.fsc_rights(day, issuer=issuer, page_size=page_size)
        except Exception as exc:
            attempt = collector.journal[-1] if len(collector.journal) > before else None
            if attempt and attempt.get("response_path"):
                path = (DEST/attempt["response_path"]).resolve()
                if not path.is_relative_to(DEST.resolve()) or digest(path.read_bytes()) != attempt["sha256"]:
                    raise ValueError("FSC error response provenance differs")
                raw, meta = path.read_bytes(), attempt
            else:
                query.update(state="request_failed", error=attempt.get("error", type(exc).__name__) if attempt else type(exc).__name__, total_count=None, rows=[])
        if raw is not None:
            files[f"raw/query-{number}.bin"] = raw
            query.update(source_sha256=digest(raw), source_response_path=meta["response_path"], http_status=meta.get("http_status"))
            try:
                query.update(parse_response(raw, day, page_size=page_size))
                if meta.get("http_status") and not 200 <= meta["http_status"] < 300:
                    query["state"] = "http_error"
            except (ValueError, TypeError, KeyError):
                query.update(state="invalid_response", rows=[], total_count=None)
        queries.append(query)
        if query["state"] in ("api_rejected", "unrecognized_response", "request_failed", "invalid_response", "http_error"):
            break
    successful = [q for q in queries if q["state"] == "page_received"]
    state = ("historical_records_received_scope_unverified" if successful else
        "source_unavailable" if any(q["state"] in ("api_rejected", "request_failed", "unrecognized_response", "invalid_response", "http_error") for q in queries)
        else "empty_historical_queries_not_scope_proof")
    report = {"schema": "kr-net-alpha-fsc-rights-probe-v1", "candidate_id": collector.spec["candidate_id"],
        "contract_hashes": collector.spec["contract_hashes"], "implementation_sha256": digest(Path(__file__).read_bytes()),
        "endpoint": FSC_RIGHTS, "policy_sha256": POLICY_SHA,
        "state": state, "queries": queries, "new_requests": len(collector.journal)-initial,
        "cumulative_action_attempts": 7+len(collector.journal),
        "economic_scope_certified": False, "strict_pit_eligible": False, "live_order_eligible": False,
        "adoption": "not_evaluable_data_gate", "integrated_progress": 40}
    files["journals/requests.json"] = json.dumps(collector.journal[initial:], ensure_ascii=False, sort_keys=True).encode()
    files["probe-report.json"] = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    files["implementation/probe.py"] = Path(__file__).read_bytes()
    files["implementation/collector.py"] = (ROOT/"scripts/measure/kr_action_source_audit.py").read_bytes()
    manifest = {"schema": "kr-net-alpha-fsc-rights-originals-v1", "entries": {
        n: {"sha256": digest(raw), "bytes": len(raw)} for n, raw in files.items()}, "economic_scope_certified": False}
    if source_storage_bytes()+sum(map(len, files.values())) > collector.spec["max_new_archive_bytes"]:
        raise ValueError("FSC archive exceeds cumulative source byte cap")
    write_archive(Path(output), files, manifest)
    report.update(archive=str(Path(output).relative_to(ROOT)), archive_sha256=digest(Path(output).read_bytes()))
    save(report_path, report)
    print(json.dumps({"state": state, "archive_sha256": report["archive_sha256"],
        "queries": [{k:q.get(k) for k in ("basis_on", "issuer_filter", "state", "result_code", "total_count")} for q in queries],
        "new_requests": report["new_requests"]}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--crosscheck", action="store_true")
    parser.add_argument("--inspect", type=Path)
    args = parser.parse_args()
    if args.inspect:
        inspect_crosscheck(args.inspect, args.report)
    elif args.output:
        (crosscheck if args.crosscheck else execute)(args.output, args.report)
    else:
        parser.error("--output or --inspect required")
