"""Isolated, frozen revenue/forward-guidance study. Never imports broker orders."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from collections import Counter
from pathlib import Path

from scripts.measure import earnings_announcement_execution as old
from scripts.measure.contract_event_sources import _json, load_archive
from scripts.measure.earnings_fast_entry_sources import _get, _save
from signal_desk.ingest.alpaca_research import clock_ns

PLAN = Path(__file__).resolve().parents[2] / "docs/EARNINGS-GUIDANCE-VALIDATION-2026-10-17.md"
YEARS = ("2020", "2021")
PANEL = old.PANEL
SECOND = old.SECOND
NUM = r"(\d+(?:\.\d+)?)"
COMPARISON = r"\s+(?:Beat[s]?|Miss(?:es)?|vs\.?|Vs\.?)\s+\$?"
EST = r"\s+Est(?:imate)?\b"
REVENUE = re.compile(r"\b(?:Sales|Revenue)\s+\$?" + NUM + r"([BM])" + COMPARISON + NUM + r"([BM])" + EST, re.I)
GUIDE = re.compile(r"\b(?:Sees|Expects)\s+Q([1-4])(?:\s+20\d\d)?\s+(?:(?:Net\s+)?Sales|Revenue)(?:\s+Of)?\s+\$?" +
    NUM + r"([BM])(?:\s*(?:-|To)\s*\$?" + NUM + r"([BM]))?(?:\s*\+/-\s*" + NUM + r"%)?" +
    r"\s+Vs\.?\s+\$?" + NUM + r"([BM])" + EST, re.I)

PRIMARY = {
    "NVDA-2023-02-22": "https://investor.nvidia.com/news/press-release-details/2023/NVIDIA-Announces-Financial-Results-for-Fourth-Quarter-and-Fiscal-2023/",
    "NVDA-2023-05-24": "https://investor.nvidia.com/news/press-release-details/2023/NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2024/default.aspx",
    "NVDA-2023-08-23": "https://investor.nvidia.com/news/press-release-details/2023/NVIDIA-Announces-Financial-Results-for-Second-Quarter-Fiscal-2024/default.aspx",
    "AAPL-2023-11-02": "https://www.apple.com/newsroom/2023/11/apple-reports-fourth-quarter-results/",
    "MSFT-2023-10-24": "https://news.microsoft.com/source/2023/10/24/microsoft-cloud-strength-fuels-first-quarter-results-3/",
    "MSFT-call-2023-10-24": "https://www.microsoft.com/en-us/Investor/events/FY-2024/earnings-fy-2024-q1.aspx",
    "AMZN-2022-10-27": "https://ir.aboutamazon.com/news-release/news-release-details/2022/Amazon-com-Announces-Third-Quarter-Results/",
    "AAPL-2022-04-28": "https://www.apple.com/newsroom/2022/04/apple-reports-second-quarter-results/",
    "NVDA-2022-05-25": "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2023",
    "JPM-2022-01-14": "https://sp.jpmorgan.com/spweb/content/download/2269517",
}


def collect_primary(output: Path):
    files, rows = {}, []
    for label, url in PRIMARY.items():
        status, raw = _get(url)
        name = "raw/" + label + ".bin"
        files[name] = raw
        rows.append({"id": label, "url": url, "http_status": status, "file": name,
                     "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    _save(output, files, {"schema": "earnings-guidance-primary-v1", "observations": rows,
                          "live_order_eligible": False})
    return {"output": str(output), "sha256": old.digest(output), "statuses": rows}


def cohort(source: Path):
    _, files = load_archive(source, "us-eps-sources-v1")
    events = []
    for symbol in PANEL:
        body = json.loads(files[f"raw/earnings-{symbol}.json"])
        if body["symbol"] != symbol:
            raise ValueError("issuer mismatch")
        for r in body["quarterlyEarnings"]:
            day = r.get("reportedDate", "")
            if "2020-01-01" <= day <= "2021-12-31":
                if dt.date.fromisoformat(r["fiscalDateEnding"]) >= dt.date.fromisoformat(day):
                    raise ValueError("fiscal period mismatch")
                events.append({"id": symbol + ":" + day, "symbol": symbol,
                               "reported_date": day, "fiscal_end": r["fiscalDateEnding"]})
    if Counter((r["symbol"], r["reported_date"][:4]) for r in events) != Counter({(s,y):4 for s in PANEL for y in YEARS}):
        raise ValueError("fixed 64 reports incomplete")
    return {"schema": "earnings-guidance-cohort-v1", "plan_sha256": old.digest(PLAN),
            "source_sha256": old.digest(source), "events": sorted(events, key=lambda e:e["id"]),
            "live_order_eligible": False}


def _amount(value, unit):
    return float(value) * (1_000_000_000 if unit.upper() == "B" else 1_000_000)


def _first(rows):
    if not rows:
        return None, False
    stamp = min(r["available_ns"] for r in rows)
    found = [r for r in rows if r["available_ns"] == stamp]
    pairs = {tuple(r[k] for k in ("actual", "expected")) for r in found}
    return min(found, key=lambda r:r["news_id"]), len(pairs) != 1


def input_events(frozen, sources):
    events = []
    for event in frozen["events"]:
        symbol, day = event["symbol"], event["reported_date"]
        aliases = {symbol, "FB"} if symbol == "META" else ({"GOOGL", "GOOG"} if symbol == "GOOGL" else {symbol})
        pages = [s for s in sources if s["symbol"] in aliases and s["request"]["start"][:10] == day]
        row = {**event, "status": "news_missing", "eligible": None}
        if not pages or any(p["body"] is None for p in pages):
            events.append(row)
            continue
        tokens = {p["request"].get("page_token", "") for p in pages}
        if "" not in tokens or any(p["body"].get("next_page_token") not in tokens | {None, ""} for p in pages):
            events.append({**row, "status": "news_pagination_incomplete"})
            continue
        # Deduplicate identical article versions, reject contradictory versions.
        articles, conflict = {}, False
        for page in pages:
            for a in page["body"]["news"]:
                if a["id"] in articles and articles[a["id"]]["article"] != a:
                    conflict = True
                articles[a["id"]] = {"article": a, "source_sha256": page["raw_sha256"]}
        if conflict:
            events.append({**row, "status": "article_version_conflict"})
            continue
        eps, reports, revenues, guides = [], [], [], []
        title = re.compile(r"^" + old.ISSUER_TITLES[symbol] + r"\s+Q([1-4])\s+(?:Adj\.?\s+)?(?:EPS\b|Sales\b|Revenue\b|Earnings Highlights\b)", re.I)
        for item in articles.values():
            a = item["article"]
            headline = a["headline"]
            if not aliases.intersection(a["symbols"]) or len(set(a["symbols"]) & set(PANEL)) > 1:
                continue
            first, changed = clock_ns(a["created_at"]), clock_ns(a["updated_at"])
            if changed < first or any(old.as_dt(t).astimezone(old.NY).date().isoformat() != day for t in (first,changed)):
                continue
            provenance = {"news_id": a["id"], "url": a["url"], "created_at": a["created_at"],
                          "updated_at": a["updated_at"], "available_ns": max(first,changed),
                          "source_sha256": item["source_sha256"]}
            marker = title.search(headline)
            if marker and not re.search(r"\b(?:Sees|Expects|Preview|Why|Recap|Yesterday|Last Week)\b",headline,re.I):
                reports.append(provenance)
                m = old.EPS.search(headline)
                if m:
                    eps.append({**provenance, "quarter": int(marker[1]), "actual": float(m[1]), "expected": float(m[2]),
                                "basis": "adjusted" if re.search(r"Adj\.?|Adjusted|Excluding Significant Items",headline,re.I) else "unspecified"})
                m = REVENUE.search(headline)
                if m and not (m.start() == marker.end()-len(marker[0].rsplit(" ",1)[-1]) or
                              re.search(r",\s*$",headline[:m.start()])):
                    # A segment-name prefix is not an issuer-total comparison.
                    m = None
                if m:
                    revenues.append({**provenance, "quarter": int(marker[1]), "actual": _amount(m[1],m[2]),
                                     "expected": _amount(m[3],m[4])})
            if re.match(old.ISSUER_TITLES[symbol] + r"\s+(?:Sees|Expects)\b",headline,re.I):
                m = GUIDE.search(headline)
                if m:
                    low = _amount(m[2],m[3]); high = _amount(m[4],m[5]) if m[4] else low
                    if m[6]:
                        if m[4] or float(m[6]) >= 100:
                            continue
                        low,high = low*(1-float(m[6])/100),high*(1+float(m[6])/100)
                    if high < low:
                        continue
                    guides.append({**provenance, "quarter": int(m[1]), "actual": (low+high)/2,
                                   "low": low, "high": high, "expected": _amount(m[7],m[8])})
        if not reports:
            events.append({**row, "status": "direct_report_missing"})
            continue
        deadline = min(r["available_ns"] for r in reports) + 600 * SECOND
        e, eps_conflict = _first([r for r in eps if r["available_ns"] <= deadline])
        rev, rev_conflict = _first([r for r in revenues if r["available_ns"] <= deadline and (not e or r["quarter"] == e["quarter"])])
        guide, guide_conflict = _first([r for r in guides if r["available_ns"] <= deadline and e and r["quarter"] == e["quarter"]%4+1])
        eps_eligible = bool(e and not eps_conflict and e["actual"] > 0 and e["expected"] > 0 and (e["actual"]/e["expected"]-1)*100 >= 5-1e-10)
        # An incomplete collection is never a known no-order; absent features in
        # a complete source day ARE an explicit abstention of this feed policy.
        events.append({**row, "status": "ready", "ready_ns": deadline, "eps": e, "revenue": rev, "guidance": guide,
                       "eps_conflict": eps_conflict, "revenue_conflict": rev_conflict, "guidance_conflict": guide_conflict,
                       "eps_eligible": eps_eligible,
                       "guidance_status": "conflict" if guide_conflict else ("unknown" if not guide else ("below_or_equal" if guide["actual"] <= guide["expected"] else "above")),
                       "eligible": bool(eps_eligible and rev and not rev_conflict and rev["actual"] > rev["expected"] > 0
                                        and not guide_conflict and (not guide or guide["actual"] > guide["expected"] > 0))})
    return {"schema": "earnings-guidance-inputs-v1", "cohort": frozen, "events": events,
            "strict_pit_eligible": False, "live_order_eligible": False}


def choices(inputs, year, policy, delay=0, excluded=None):
    if year not in YEARS or policy not in ("guidance", "eps", "market", "cash") or delay not in (0,60):
        raise ValueError("outside frozen guidance study")
    candidates = []
    for e in inputs["events"]:
        if (e["status"] != "ready" or e["reported_date"][:4] != year or e["symbol"] == excluded
                or not e["eps_eligible" if policy == "eps" else "eligible"]):
            continue
        stamp = old.decision(e, "immediate", delay)
        if stamp is not None:
            candidates.append({**e, "decision_ns":stamp})
    selected = {}
    for e in sorted(candidates,key=lambda e:(e["decision_ns"],e["symbol"])):
        selected.setdefault(old.as_dt(e["decision_ns"]).astimezone(old.NY).date().isoformat(),e)
    return list(selected.values())


def simulate(inputs,index,year,policy,fee=10,fill=1,delay=0,scale=100,excluded=None):
    if fee not in (10,25,50) or fill not in (0,.5,1) or scale not in (1,100) or excluded not in set(PANEL)|{None}:
        raise ValueError("outside frozen execution assumptions")
    cash, rows, needed, stopped = 10_000., [], {}, False
    for e in choices(inputs,year,policy,delay,excluded):
        row = {"id":e["id"],"issuer":e["symbol"],"decision_ns":e["decision_ns"],
               "quarter":(int(e["reported_date"][5:7])-1)//3+1}
        if stopped:
            rows.append({**row,"status":"blocked_by_unresolved_prior_execution","net_pnl":None})
            continue
        if policy == "cash" or fill == 0:
            rows.append({**row,"status":"no_order","net_pnl":0.})
            continue
        symbol = "SPY" if policy == "market" else e["symbol"]
        def quote(ns,side="ap",quantity=1):
            q,need = old.quote_or_need(index,old.query(symbol,ns),ns,side,quantity)
            if need:
                # Large sparse windows keep the same row/byte/time cap; a
                # partial page is narrowed by old.quote_or_need, never skipped.
                if clock_ns(need["end"])-clock_ns(need["start"]) == 5*SECOND:
                    need = old.query(symbol,clock_ns(need["start"]),300)
                needed[old.key(need)] = need
            return q
        ref = quote(e["decision_ns"])
        if ref is None:
            rows.append({**row,"status":"reference_missing","net_pnl":None});stopped=True
            continue
        rate = fee/10_000
        limit = float(ref["ap"])*1.003
        qty = math.floor(min(cash/6/(limit*(1+rate)),float(ref["as"])*scale)*fill)
        if qty == 0:
            rows.append({**row,"status":"no_affordable_whole_share","net_pnl":0.})
            continue
        executable = ((ref["ns"]+2*SECOND-1)//SECOND)*SECOND
        entry = quote(executable)
        if entry is None:
            rows.append({**row,"status":"entry_missing","net_pnl":None});stopped=True
            continue
        if float(entry["ap"]) > limit:
            rows.append({**row,"status":"limit_not_marketable","net_pnl":0.})
            continue
        qty = min(qty,int(entry["as"])*scale)
        cost = qty*float(entry["ap"])*(1+rate)
        if cost > cash+1e-8:
            raise ValueError("cash constraint broken")
        exit_ns = entry["ns"]+3600*SECOND
        exit_quote = quote(exit_ns,"bp",math.ceil(qty/scale))
        if exit_quote is None:
            rows.append({**row,"status":"exit_missing_position_unresolved","net_pnl":None,
                         "quantity":qty,"entry_ns":entry["ns"]});stopped=True
            continue
        ask,bid = float(entry["ap"]),float(exit_quote["bp"])
        gross = qty*(bid-ask);fees=qty*(bid+ask)*rate
        pnl = qty*bid*(1-rate)-cost
        mid = qty*((bid+float(exit_quote["ap"]))-(ask+float(entry["bp"])))/2
        spread = qty*((ask-float(entry["bp"]))+(float(exit_quote["ap"])-bid))/2
        if abs(pnl-(gross-fees))>1e-8 or abs(gross-(mid-spread))>1e-8:
            raise ValueError("cash/PnL reconciliation failed")
        cash+=pnl
        rows.append({**row,"symbol":symbol,"status":"closed","quantity":qty,"entry_ns":entry["ns"],
                     "exit_ns":exit_quote["ns"],"entry_ask":ask,"exit_bid":bid,
                     "reference_lag_seconds":(ref["ns"]-e["decision_ns"])/SECOND,
                     "entry_after_reference_seconds":(entry["ns"]-ref["ns"])/SECOND,
                     "exit_after_target_seconds":(exit_quote["ns"]-exit_ns)/SECOND,
                     "reference_sha256":ref["source_sha256"],"entry_sha256":entry["source_sha256"],
                     "exit_sha256":exit_quote["source_sha256"],"net_pnl":pnl,"gross_pnl":gross,
                     "fees":fees,"mid_price_pnl":mid,"spread_drag":spread,"trade_net_return_pct":pnl/(qty*ask)*100})
    missing = [e["id"] for e in inputs["events"] if e["reported_date"][:4]==year and e["status"]!="ready"]
    unresolved = [r["id"] for r in rows if r["net_pnl"] is None]
    trades=[r for r in rows if r["status"]=="closed"]
    complete=not missing and not unresolved
    return {"year":year,"policy":policy,"fee_bp_each_side_assumed":fee,"fill_fraction":fill,
            "extra_delay_seconds":delay,"shares_per_raw_size_unit_assumed":scale,"excluded_issuer":excluded,
            "complete":complete,"missing_input_events":missing,"unresolved_executions":unresolved,
            "selected_events":len(rows),"closed_trades":len(trades),"issuers":len({r["issuer"] for r in trades}),
            "conditional_account_return_pct":(cash/10_000-1)*100 if complete else None,
            "actual_historical_net_return_pct":None,
            "quarter_trade_counts":{str(q):sum(r["quarter"]==q for r in trades) for q in range(1,5)},
            "quarter_pnl":{str(q):sum(r["net_pnl"] for r in trades if r["quarter"]==q) for q in range(1,5)},
            "issuer_pnl":{s:sum(r["net_pnl"] for r in trades if r["issuer"]==s) for s in PANEL},
            "gross_pnl":sum(r["gross_pnl"] for r in trades),"assumed_fees":sum(r["fees"] for r in trades),
            "spread_drag":sum(r["spread_drag"] for r in trades),"rows":rows,
            "needed_requests":[r for _,r in sorted(needed.items())]}


def report(inputs,index):
    if inputs["cohort"]["plan_sha256"] != old.digest(PLAN):
        raise ValueError("frozen plan changed")
    scenarios = [("guidance",10,1,0),("eps",10,1,0),("market",10,1,0),("cash",10,1,0),
                 ("guidance",25,1,0),("guidance",50,1,0),("guidance",10,1,60),
                 ("guidance",10,.5,0),("guidance",10,0,0)]
    results = [simulate(inputs,index,y,p,f,fill,d,scale) for scale in (100,1) for y in YEARS for p,f,fill,d in scenarios]
    exclusions = [simulate(inputs,index,"2021","guidance",excluded=s) for s in PANEL]
    confirm = [r for r in results if r["year"]=="2021" and r["shares_per_raw_size_unit_assumed"]==100]
    base = confirm[0];controls=confirm[1:4]
    stress = [confirm[i] for i in (4,6,7)]
    positive = [max(0,v) for v in base["issuer_pnl"].values()]
    share=max(positive)/sum(positive) if sum(positive)>0 else None
    checks = {"all_sources_complete":all(r["complete"] for r in results+exclusions),
              "minimum_trades":base["closed_trades"]>=12,"minimum_issuers":base["issuers"]>=6,
              "quarter_counts":all(n>=2 for n in base["quarter_trade_counts"].values()),
              "all_quarters_positive":all(v>0 for v in base["quarter_pnl"].values()),
              "positive_and_beats_controls":base["complete"] and base["conditional_account_return_pct"]>0 and
                  all(r["complete"] and base["conditional_account_return_pct"]>r["conditional_account_return_pct"] for r in controls),
              "stress_positive":all(r["complete"] and r["conditional_account_return_pct"]>0 for r in stress),
              "positive_issuer_concentration_at_most_half":share is not None and share<=.5,
              "leave_one_issuer_out_nonnegative":all(r["complete"] and r["conditional_account_return_pct"]>=0 for r in exclusions)}
    needs = {old.key(q):q for r in results+exclusions for q in r["needed_requests"]}
    return {"schema":"earnings-guidance-report-v1","cohort":inputs["cohort"],"event_ledger":inputs["events"],
            "results":results,"leave_one_issuer_out":exclusions,"largest_positive_issuer_share":share,"checks":checks,
            "verdict":"passed_conditional_reconstruction_only" if all(checks.values()) else
                      ("incomplete_sources" if not checks["all_sources_complete"] else "rejected_performance"),
            "adoption":"not_adopted","live_order_eligible":False,"strict_pit_eligible":False,
            "needed_requests":[r for _,r in sorted(needs.items())],
            "limitations":["historical_arrival_and_original_versions_unverified","actual_settlement_costs_unverified",
                           "fixed_survivor_panel","historical_size_unit_interpretation_not_fill_certification",
                           "guidance_unknown_is_not_positive_guidance","drawdown_not_intraholding_measured"]}


def requests(inputs,index):
    result=report(inputs,index)
    needed={old.key(q):q for q in result["needed_requests"]}
    for year in YEARS:
        for policy,delay in (("eps",0),("guidance",0),("guidance",60),("market",0)):
            for excluded in (None,*PANEL):
                for event in choices(inputs,year,policy,delay,excluded):
                    symbol="SPY" if policy=="market" else event["symbol"]
                    ns=event["decision_ns"]
                    for stage in range(3):
                        found,need=old.quote_or_need(index,old.query(symbol,ns),ns,"bp" if stage==2 else "ap")
                        if need:
                            if clock_ns(need["end"])-clock_ns(need["start"])==5*SECOND:
                                need=old.query(symbol,clock_ns(need["start"]),300)
                            needed[old.key(need)]=need
                        if found is None:
                            break
                        ns=((found["ns"]+2*SECOND-1)//SECOND)*SECOND if stage==0 else found["ns"]+3600*SECOND
    return [r for _,r in sorted(needed.items())]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("primary", "cohort", "inputs", "needs", "report"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--sources", nargs="*", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "primary":
        print(json.dumps(collect_primary(args.output)))
        return
    if args.command == "cohort":
        result = cohort(args.source)
    elif args.command == "inputs":
        result = input_events(json.loads(args.input.read_bytes()), old.observations(args.sources,"news-day"))
    else:
        inputs=json.loads(args.input.read_bytes())
        index=old.quote_index(old.observations(args.sources,"quote-window"))
        result=({"schema":"earnings-guidance-requests-v1","requests":requests(inputs,index)}
                if args.command=="needs" else report(inputs,index))
    old.save(args.output, result)
    print(json.dumps({"output":str(args.output), "sha256":old.digest(args.output),
                      "events":len(result.get("events",[])), "statuses":dict(Counter(e.get("status") for e in result.get("events",[])))}))


if __name__ == "__main__":
    main()
