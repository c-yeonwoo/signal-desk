"""Freeze local licensed sources and publish price-free reproducible summaries."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from scripts.measure import earnings_announcement_execution as old
from scripts.measure import earnings_guidance_validation as study
from scripts.measure.contract_event_sources import load_archive
from scripts.measure.earnings_fast_entry_sources import _save
from signal_desk import market_clock
from signal_desk.ingest import alpaca_research


def code_paths():
    return [Path(__file__),Path(study.__file__),Path(old.__file__),Path(alpaca_research.__file__),
            Path(market_clock.__file__),Path(__file__).with_name("us_earnings_backtest.py"),
            Path(__file__).with_name("earnings_guidance_audit.py")]


def public_summary(report):
    result={k:v for k,v in report.items() if k not in ("results","leave_one_issuer_out","needed_requests")}
    for name in ("results","leave_one_issuer_out"):
        result[name]=[{k:v for k,v in r.items() if k not in ("rows","needed_requests")} for r in report[name]]
    return result


def _decode(files,kind):
    sources=[]
    for name,raw_file in sorted(files.items()):
        if not name.startswith("observations/"+kind+"/"):
            continue
        artifact=json.loads(raw_file)
        for row in artifact["rows"]:
            s=row["body"]
            if s.get("schema") != "alpaca-historical-"+("news-day" if kind=="news" else "quote-window")+"-v1":
                raise ValueError("wrong preserved source kind")
            if row["gateway_http_status"] != 200 or s["status"] not in ("observed","empty"):
                sources.append({**s,"body":None});continue
            raw=base64.b64decode(s["raw_b64"],validate=True)
            if len(raw)!=s["raw_bytes"] or hashlib.sha256(raw).hexdigest()!=s["raw_sha256"]:
                raise ValueError("preserved raw source integrity failed")
            sources.append({**s,"body":json.loads(raw)})
    return sources


def replay(archive):
    _,files=load_archive(archive,"earnings-guidance-source-bundle-v1")
    for p in code_paths():
        if files["code/"+p.name]!=p.read_bytes():
            raise ValueError("checkout frozen source code before replay")
    cohort=json.loads(files["cohort.json"]);inputs=json.loads(files["inputs.json"])
    if hashlib.sha256(files["plan.md"]).hexdigest()!=cohort["plan_sha256"]:
        raise ValueError("archive plan mismatch")
    regenerated=study.input_events(cohort,_decode(files,"news"))
    if regenerated!=inputs:
        raise ValueError("source regeneration differs from frozen inputs")
    return study.report(inputs,old.quote_index(_decode(files,"quotes")))


def deliver(inputs_path,news_paths,quote_paths,primary_paths,archive,report_path,public_path):
    inputs=json.loads(inputs_path.read_bytes())
    news=old.observations(news_paths,"news-day");quotes=old.observations(quote_paths,"quote-window")
    if study.input_events(inputs["cohort"],news)!=inputs:
        raise ValueError("inputs differ from source regeneration")
    result=study.report(inputs,old.quote_index(quotes))
    files={"plan.md":study.PLAN.read_bytes(),"inputs.json":inputs_path.read_bytes(),
           "cohort.json":json.dumps(inputs["cohort"],ensure_ascii=False,sort_keys=True,allow_nan=False).encode()}
    for kind,paths in (("news",news_paths),("quotes",quote_paths),("primary",primary_paths)):
        for p in paths:
            key="observations/"+kind+"/"+p.name
            if key in files:
                raise ValueError("duplicate source artifact name")
            files[key]=p.read_bytes()
    code=code_paths()
    for p in code:
        files["code/"+p.name]=p.read_bytes()
    _save(archive,files,{"schema":"earnings-guidance-source-bundle-v1","live_order_eligible":False})
    result.update(source_archive=str(archive),source_archive_sha256=old.digest(archive),
                  code_sha256={p.name:old.digest(p) for p in code},
                  preserved_sources={"news_envelopes":len(news),
                      "news_response_rows":sum(len(s["body"]["news"]) for s in news if s["body"]),
                      "unique_news_ids":len({a["id"] for s in news if s["body"] for a in s["body"]["news"]}),
                      "quote_envelopes":len(quotes),"unique_quote_windows":len(old.quote_index(quotes)),
                      "quote_response_rows_including_overlap":sum(len(s["body"]["quotes"] or []) for s in quotes if s["body"]),
                      "failed_quote_envelopes":sum(s["body"] is None for s in quotes),
                      "empty_quote_envelopes":sum(s["status"]=="empty" for s in quotes)})
    old.save(report_path,result)
    public=public_summary(result)
    public.update(private_report=str(report_path),private_report_sha256=old.digest(report_path))
    old.save(public_path,public)
    return {"verdict":result["verdict"],"checks":result["checks"],"archive_sha256":old.digest(archive),
            "report_sha256":old.digest(report_path),"public_sha256":old.digest(public_path),
            "needed_requests":len(result["needed_requests"]),"preserved_sources":result["preserved_sources"]}


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument("command",choices=("deliver","replay"))
    p.add_argument("--input",type=Path);p.add_argument("--news",nargs="*",type=Path,default=[])
    p.add_argument("--quotes",nargs="*",type=Path,default=[]);p.add_argument("--primary",nargs="*",type=Path,default=[])
    p.add_argument("--archive",type=Path,required=True);p.add_argument("--report",type=Path,required=True)
    p.add_argument("--public",type=Path);a=p.parse_args()
    if a.command=="deliver":
        print(json.dumps(deliver(a.input,a.news,a.quotes,a.primary,a.archive,a.report,a.public)))
    else:
        r=replay(a.archive);old.save(a.report,r)
        print(json.dumps({"verdict":r["verdict"],"report_sha256":old.digest(a.report)}))


if __name__=="__main__":
    main()
