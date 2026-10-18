"""Bounded public SEIBro calendar/detail crosscheck, not a scope certificate."""

import argparse
import datetime as dt
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from scripts.measure.acquire_historical_sources import write_archive
from scripts.measure.kr_action_source_audit import Collector
from scripts.measure.kr_net_alpha_sources import ROOT, DEST, digest
from scripts.measure.kr_net_alpha_v2 import evidence, save, source_storage_bytes

FORMS = {
    "calendar":"https://seibro.or.kr/IPORTAL/user/company/BIP_CNTS01021V.xml",
    "split":"https://seibro.or.kr/IPORTAL/user/company/BIP_CNTS01028V.xml",
    "dividend":"https://seibro.or.kr/IPORTAL/user/company/BIP_CNTS01027V.xml"}
FORM_HASHES = {"calendar":"efb35f521c26d87a4c4e2e97ceb5cff2c6c9b0a149c6c18e013559891b8f4370",
    "split":"e62939fdbaa7c4d443f7bd0761804165766a42b47791159cfd36baf62e150233",
    "dividend":"5dbb75b5aabeb71b715f7159112c3a50df149d4eff5e07c1118b06c403e0186f"}
PACKET = ROOT/"data/research/kr-net-alpha-action-clock-packet-2026-10-18-v2.json"


def parse_response(raw):
    if len(raw) > 5_000_000 or b"<!" in raw:
        raise ValueError("unapproved SEIBro response")
    root = ET.fromstring(raw)
    if root.tag not in ("vector", "result") or not root.get("result", "").isdigit():
        raise ValueError("SEIBro response is not a declared result")
    if root.tag == "vector" and any(n.tag != "data" or len(n) != 1 or n[0].tag != "result" for n in root):
        raise ValueError("SEIBro vector contains unrecognized records")
    nodes = [root] if root.tag == "result" else root.findall("./data/result")
    rows = []
    for node in nodes:
        if (len({n.tag for n in node}) != len(node) or not len(node)
                or any(set(n.attrib) != {"value"} or len(n) for n in node)):
            raise ValueError("SEIBro row shape ambiguous")
        rows.append({n.tag:n.get("value") for n in node})
    if int(root.get("result")) != len(rows):
        raise ValueError("SEIBro declared count differs")
    return {"rows":rows, "declared_count":int(root.get("result")),
        "continuation":root.get("next"),
        "duplicate_rows":len(rows)-len({json.dumps(r,sort_keys=True) for r in rows}),
        "economic_scope_certified":False}


def ordinary(rows, ticker):
    matches = [r for r in rows if r.get("SHOTN_ISIN") == ticker and r.get("STK_KACD") == "보통주"]
    if len(matches) != 1:
        return {"state":"ordinary_share_unresolved", "rows":matches}
    return {"state":"selected_ordinary_share_not_scope_certificate", "row":matches[0]}


def partition_check(whole, parts):
    # SCH_DTTM is the current query clock, not a historical revision/publication.
    def keyed(rows):
        return {json.dumps({k:v for k,v in r.items() if k != "SCH_DTTM"}, sort_keys=True,ensure_ascii=False) for r in rows}
    full, split = keyed(whole), keyed(parts)
    return {"whole_unique_rows":len(full), "partition_unique_rows":len(split),
        "whole_only_rows":[json.loads(r) for r in sorted(full-split)],
        "partition_only_rows":[json.loads(r) for r in sorted(split-full)],
        "consistent":full == split, "economic_scope_certified":False}


def run(output, report_path):
    output, report_path = Path(output).resolve(), Path(report_path).resolve()
    if any(not p.is_relative_to((ROOT/"data/research").resolve()) or p.exists() for p in (output,report_path)):
        raise ValueError("requires new local research outputs")
    c = Collector()
    before = len(c.journal)
    files, queries = {}, []
    evidence(PACKET)  # Keep the already audited identity/source packet intact.
    bindings = {r["ticker"]:r["security_id"] for r in json.loads(PACKET.read_bytes())["identity_bindings"]}
    for name,url in FORMS.items():
        raw,meta = c.get(url)
        if digest(raw) != FORM_HASHES[name]:
            raise ValueError("public SEIBro query form changed; review before requests")
        files[f"forms/{name}.xml"] = raw

    def keep(case, raw, meta, **query):
        name = f"raw/{case}.xml"; files[name] = raw
        parsed = parse_response(raw)
        queries.append({"case_id":case, "query":query, "source_sha256":meta["sha256"],
            "observed_at":meta["observed_at"], "raw_member":name, **parsed})

    calendars = (("april-rights-market","2021-04-12","2021-04-18","03",""),
        ("march-rights-market","2021-03-29","2021-04-04","03",""),
        ("samsung-payment-week","2021-05-17","2021-05-23","10",""),
        ("kakao-year-all-roles","2021-01-01","2021-12-31","","3572"),
        ("kakao-first-half-all-roles","2021-01-01","2021-06-30","","3572"),
        ("kakao-second-half-all-roles","2021-07-01","2021-12-31","","3572"))
    for case,start,end,role,issuer in calendars:
        raw,meta = c.seibro_calendar(start,end,date_role=role,issuer=issuer)
        keep(case,raw,meta,start=start,end=end,date_role=role,issuer=issuer)
    cases = (("samsung","005930","593","2021-03-31","103"),
        ("kakao","035720","3572","2021-04-13","201"),
        ("hyundai","267250","29893","2021-04-11","201"))
    selected = {}
    for case,ticker,issuer,record,reason in cases:
        actions = ("divSkedulView","issuDetailsList2","payDetailsList1") if reason == "103" else (
            "faceDiviMergBasicInfoListEL1","issuDetailsList3","payDetailsList2")
        for action in actions:
            raw,meta = c.seibro_detail(action,issuer,record,reason)
            keep(case+"-"+action,raw,meta,action=action,issuer=issuer,record_on=record,reason=reason)
            if action.startswith("issuDetails"):
                selected[case] = {"ticker":ticker,"historical_security_id":bindings[ticker],
                    "binding_packet_sha256":digest(PACKET.read_bytes()), **ordinary(queries[-1]["rows"],ticker)}
    used = 7+len(c.journal)
    market_weeks = math.ceil(((dt.date.fromisoformat(c.spec["liquidation_through"])-
        dt.date.fromisoformat(c.spec["warmup_from"])).days+1)/7)
    calendar_rows = {q["case_id"]:q["rows"] for q in queries}
    result = {"schema":"kr-net-alpha-seibro-source-review-v1", "candidate_id":c.spec["candidate_id"],
        "contract_hashes":c.spec["contract_hashes"], "queries":queries, "ordinary_crosschecks":selected,
        "new_requests":len(c.journal)-before, "cumulative_action_attempts":used,
        "market_calendar_windows_full_period":market_weeks,
        "remaining_action_requests":c.spec["max_action_source_requests"]-used,
        "calendar_partition":partition_check(calendar_rows["kakao-year-all-roles"],
            calendar_rows["kakao-first-half-all-roles"]+calendar_rows["kakao-second-half-all-roles"]),
        "calendar_query_not_full_action_census":True,
        "historical_publication_revision_inventory_verified":False,
        "payment_completed_verified":False, "scope_certificate_eligible":False,
        "same_upstream_as_fsc_and_kis":"KSD", "fit_executed":False, "backtest_executed":False,
        "adoption":"not_evaluable_data_gate", "integrated_progress":40,
        "implementation_sha256":{p:digest((ROOT/p).read_bytes()) for p in (
            "scripts/measure/kr_action_source_audit.py", "scripts/measure/kr_net_alpha_seibro_scope.py")}}
    files["review.json"] = json.dumps(result,ensure_ascii=False,sort_keys=True).encode()
    files["journal.json"] = json.dumps(c.journal,ensure_ascii=False,sort_keys=True).encode()
    for p in result["implementation_sha256"]:
        files["implementation/"+Path(p).name] = (ROOT/p).read_bytes()
    if source_storage_bytes()+sum(map(len,files.values())) > c.spec["max_new_archive_bytes"]:
        raise ValueError("SEIBro archive exceeds remaining byte budget")
    write_archive(output,files,{"schema":"kr-net-alpha-seibro-originals-v1", "entries":{
        n:{"sha256":digest(raw),"bytes":len(raw)} for n,raw in files.items()}, "economic_scope_certified":False})
    result.update(archive=str(output.relative_to(ROOT)),archive_sha256=digest(output.read_bytes()))
    save(report_path,result)
    print(json.dumps({k:result[k] for k in ("new_requests","cumulative_action_attempts","archive_sha256",
        "market_calendar_windows_full_period","scope_certificate_eligible")},ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--report",type=Path,required=True)
    args = parser.parse_args()
    run(args.output,args.report)
