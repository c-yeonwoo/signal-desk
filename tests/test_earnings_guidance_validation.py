"""Only safety-critical causal source/selection/execution contracts."""
import copy

from scripts.measure import earnings_guidance_validation as g
from scripts.measure import earnings_announcement_execution as old
from signal_desk.ingest.alpaca_research import clock_ns


def article(identifier,headline,clock="2021-05-26T20:00:00Z",updated=None):
    return {"id":identifier,"headline":headline,"created_at":clock,"updated_at":updated or clock,
            "symbols":["NVDA"],"url":"https://example.test/"+str(identifier)}


def inputs(news):
    frozen={"events":[{"id":"NVDA:2021-05-26","symbol":"NVDA","reported_date":"2021-05-26","fiscal_end":"2021-04-30"}],
            "plan_sha256":old.digest(g.PLAN)}
    pages=[{"symbol":"NVDA","request":{"start":"2021-05-26T00:00:00-04:00"},
            "body":{"news":news,"next_page_token":None},"raw_sha256":"source-hash"}]
    return g.input_events(frozen,pages)


def base():
    return [article(1,"NVIDIA Q1 Adj. EPS $1.10 Beats $1.00 Estimate, Sales $6.0B Beat $5.0B Estimate")]


def test_revenue_is_required_and_future_revisions_cannot_veto_past_decision():
    row=inputs([article(1,"NVIDIA Q1 EPS $1.10 Beats $1.00 Estimate")])["events"][0]
    assert row["eps_eligible"] and not row["eligible"]
    news=base()+[article(2,"NVIDIA Sees Q2 Revenue $4B +/- 2% Vs $5B Est",updated="2021-05-26T20:11:00Z")]
    row=inputs(news)["events"][0]
    assert row["eligible"] and row["guidance_status"]=="unknown"
    assert row["ready_ns"]==clock_ns("2021-05-26T20:10:00Z")
    news[1]["updated_at"]="2021-05-26T20:09:00Z"
    row=inputs(news)["events"][0]
    assert not row["eligible"] and row["guidance_status"]=="below_or_equal"
    assert row["guidance"]["low"]==3_920_000_000 and row["guidance"]["high"]==4_080_000_000


def test_only_same_quarter_total_sales_and_next_quarter_guidance_match():
    news=base()+[article(2,"NVIDIA Sees Q3 Revenue $4B Vs $5B Est"),
                 article(3,"NVIDIA Sees Q2 Data Center Revenue $4B Vs $5B Est")]
    row=inputs(news)["events"][0]
    assert row["eligible"] and row["guidance_status"]=="unknown"
    row=inputs([article(1,"NVIDIA Q1 EPS $1.10 Beats $1.00 Estimate"),
                article(2,"NVIDIA Q2 Sales $6B Beat $5B Estimate")])["events"][0]
    assert row["eps_eligible"] and not row["eligible"]
    assert g.GUIDE.search("NVIDIA Sees Q2 Revenue $4B-$6B Vs $5.5B Est").groups()==("2","4","B","6","B",None,"5.5","B")


def test_conflicting_simultaneous_numbers_and_incomplete_pages_are_not_fills():
    row=inputs(base()+[article(2,"NVIDIA Q1 EPS $1.20 Beats $1.00 Estimate")])["events"][0]
    assert row["eps_conflict"] and not row["eps_eligible"]
    data=inputs(base());frozen=data["cohort"]
    assert g.input_events(frozen,[])["events"][0]["status"]=="news_missing"
    news=base()+[article(2,"NVIDIA Sees Q2 Revenue $6B Vs $5B Est"),
                 article(3,"NVIDIA Sees Q2 Revenue $4B Vs $5B Est")]
    row=inputs(news)["events"][0]
    assert row["guidance_conflict"] and not row["eligible"]
    news=base();news[0]["symbols"].append("FB")
    assert inputs(news)["events"][0]["status"]=="direct_report_missing"


def add_quote(index,ns,bid,ask,size=100):
    request=old.query("NVDA",ns)
    index[old.key(request)]={"body":{"symbol":"NVDA","quotes":[{
        "t":old.as_dt(ns).isoformat(),"bp":bid,"ap":ask,"bs":size,"as":size,"c":["R"],"bx":"V","ax":"V"}]},
        "has_next_page":False,"raw_sha256":"quote-hash"}


def test_next_quote_bid_ask_fees_and_unresolved_position_stop_future_book():
    data=inputs(base());ns=data["events"][0]["ready_ns"]
    index={};add_quote(index,ns,99,100);add_quote(index,ns+g.SECOND,99,100)
    add_quote(index,ns+3601*g.SECOND,102,103)
    result=g.simulate(data,index,"2021","guidance")
    t=result["rows"][0]
    assert result["complete"] and t["entry_ns"]>=ns+g.SECOND
    assert abs(t["net_pnl"]-(t["gross_pnl"]-t["fees"]))<1e-8
    assert abs(t["gross_pnl"]-(t["mid_price_pnl"]-t["spread_drag"]))<1e-8
    later=copy.deepcopy(data["events"][0]);later.update(id="NVDA:2021-05-27",reported_date="2021-05-27",ready_ns=ns+86400*g.SECOND)
    data["events"].append(later)
    index.pop(old.key(old.query("NVDA",ns+3601*g.SECOND)))
    result=g.simulate(data,index,"2021","guidance")
    assert not result["complete"] and result["conditional_account_return_pct"] is None
    assert result["rows"][0]["status"]=="exit_missing_position_unresolved"
    assert result["rows"][1]["status"]=="blocked_by_unresolved_prior_execution"


def test_issuer_exclusion_reselects_instead_of_subtracting_contributions():
    data=inputs(base());second=copy.deepcopy(data["events"][0]);second.update(symbol="MSFT",id="MSFT:2021-05-26")
    data["events"].append(second)
    assert g.choices(data,"2021","guidance")[0]["symbol"]=="MSFT"
    assert g.choices(data,"2021","guidance",excluded="MSFT")[0]["symbol"]=="NVDA"


def test_plan_hash_mutation_fails_before_result():
    import pytest
    data=inputs(base());data["cohort"]["plan_sha256"]="changed"
    with pytest.raises(ValueError,match="frozen plan changed"):
        g.report(data,{})


def test_lookup_cache_preserves_side_size_and_does_not_repeat_source_scans(monkeypatch):
    calls=[]
    def original(index,request,ns,side,qty):
        calls.append((side,qty))
        return None,request
    monkeypatch.setattr(old,"quote_or_need",original)
    lookup=g.cached_lookup({});ns=clock_ns("2021-05-26T20:10:00Z")
    assert lookup("NVDA",ns)==lookup("NVDA",ns)
    lookup("NVDA",ns,"bp",1);lookup("NVDA",ns,"bp",2)
    assert calls==[("ap",1),("bp",1),("bp",2)]


def test_public_summary_never_exposes_individual_quote_prices_or_requests():
    from scripts.measure.earnings_guidance_delivery import public_summary
    r={"results":[{"complete":True,"rows":[{"entry_ask":123}],"needed_requests":[{}]}],
       "leave_one_issuer_out":[{"rows":[{"exit_bid":124}],"needed_requests":[]}],"needed_requests":[{}]}
    assert public_summary(r)=={"results":[{"complete":True}],"leave_one_issuer_out":[{}]}


def test_unknown_historical_condition_is_blocker_not_zero_return_or_requery_loop(monkeypatch):
    data=inputs(base());ns=data["events"][0]["ready_ns"]
    import hashlib
    monkeypatch.setattr(g,"COHORT_SHA256",hashlib.sha256(g._json(data["cohort"])).hexdigest())
    index={};add_quote(index,ns,99,100)
    index[next(iter(index))]["body"]["quotes"][0]["c"]=["?"]
    quality=g.source_quality(index)
    assert quality["blocked"] and quality["response_row_conditions_including_overlap"]["2021"]["?"]==1
    assert g.requests(data,index)==[]
    result=g.report(data,index)
    assert result["verdict"]=="blocked_source_quality" and result["adoption"]=="not_adopted"
    assert not result["performance_gates_evaluated"] and result["checks"]["minimum_trades"] is None
    assert result["results"][9]["conditional_account_return_pct"] is None


def test_frozen_64_report_cohort_cannot_be_replaced_by_a_convenient_small_sample():
    import pytest
    with pytest.raises(ValueError,match="64-report cohort changed"):
        g.report(inputs(base()),{})
