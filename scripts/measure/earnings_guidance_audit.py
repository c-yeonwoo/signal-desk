"""Primary-release follow-ups and fixed legacy-study contribution audit."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from scripts.measure import earnings_announcement_execution as old
from scripts.measure.earnings_fast_entry_sources import _get, _save

PRIMARY = {
    "JPM-2022-01-14-release": "https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/quarterly-earnings/2021/4th-quarter/ae462261-4964-48c6-b947-4b69963d1143.pdf",
    "MSFT-2022-01-25": "https://www.microsoft.com/en-us/Investor/earnings/FY-2022-Q2/press-release-webcast/",
    "NVDA-2022-02-16": "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2022",
    "XOM-2022-10-28": "https://corporate.exxonmobil.com/news/news-releases/2022/1028_exxonmobil-announces-third-quarter-2022-results",
    "XOM-2022-10-28-issuer-10q": "https://investor.exxonmobil.com/sec-filings/all-sec-filings/content/0000034088-22-000064/xom-20220930.htm",
    "JPM-2023-01-13": "https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/quarterly-earnings/2022/4th-quarter/3958ba8d-cf62-4c8e-a06b-b28ed286df8e.pdf",
    "AMZN-2023-04-27": "https://ir.aboutamazon.com/news-release/news-release-details/2023/Amazon-com-Announces-First-Quarter-Results/",
    "AAPL-2023-05-04": "https://www.apple.com/newsroom/2023/05/apple-reports-second-quarter-results/",
    "ALPACA-historical-condition-explanation": "https://forum.alpaca.markets/t/where-can-i-find-trade-conditions-meaning/5935.json",
}


def collect(output,only=None):
    files,rows={},[]
    for identifier,url in PRIMARY.items():
        if only is not None and identifier not in only:
            continue
        status,raw=_get(url);name="raw/"+identifier+".bin";files[name]=raw
        rows.append({"id":identifier,"url":url,"http_status":status,"file":name,
                     "observed_at":dt.datetime.now(dt.timezone.utc).isoformat()})
    _save(output,files,{"schema":"earnings-guidance-primary-followup-v1","observations":rows})
    return {"output":str(output),"sha256":old.digest(output),"observations":rows}


def attribution(source):
    original=json.loads(source.read_bytes())
    report=original["round_lot_interpretation_sensitivity"]
    selected=[r for r in report["results"] if r["path"]=="immediate" and r["policy"]=="eps"
              and r["fee_bp_each_side_assumed"]==10 and r["fill_fraction"]==1 and r["extra_delay_seconds"]==0]
    summaries=[]
    for result in selected:
        trades=[r for r in result["rows"] if r["status"]=="closed"]
        total=sum(r["net_pnl"] for r in trades)
        issuers={s:sum(r["net_pnl"] for r in trades if r["issuer"]==s) for s in old.PANEL}
        summaries.append({"year":result["year"],"net_pnl":total,"issuer_pnl":issuers,
                          "nvda_net_contribution_share":issuers["NVDA"]/total if total else None,
                          "quarter_pnl":result["quarter_closed_subset_pnl"],
                          "quarter_counts":result["quarter_trade_counts"],
                          "cases":[{k:v for k,v in t.items() if k not in ("entry_ask","exit_bid")}
                                   for t in trades if t["net_pnl"]<0 or t["issuer"]=="NVDA"]})
    return {"schema":"earnings-guidance-legacy-attribution-v1","source_sha256":old.digest(source),
            "size_interpretation":"round_lot100_post_source_clarification_not_new_confirmation",
            "summaries":summaries,"causal_effect_identified":False,"live_order_eligible":False}


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument("command",choices=("primary","attribution"))
    p.add_argument("--source",type=Path);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--only",nargs="+",choices=sorted(PRIMARY));a=p.parse_args()
    if a.command=="primary":
        print(json.dumps(collect(a.output,a.only)))
    else:
        result=attribution(a.source);old.save(a.output,result)
        print(json.dumps({"output":str(a.output),"sha256":old.digest(a.output)}))


if __name__=="__main__":
    main()
