"""Display contracts: unknown dates and illustrations must not look like facts."""

import json
import subprocess
from pathlib import Path

import pandas as pd

from signal_desk import store
from signal_desk.signals import hypo_score
from signal_desk.signals import hypothesis


HTML_PATH = Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html"


def test_generated_watch_tickers_are_preserved_for_future_scoring():
    tree = {"tickers": ["A"], "children": [{"watch_tickers": [
        {"ticker": "A"}, {"ticker": "B"}, {"ticker": None}]}]}
    assert hypothesis._tree_tickers(tree) == ["A", "B"]


def test_nullable_prices_preserve_dates_for_issue_scoring(tmp_path, monkeypatch):
    frame = pd.DataFrame({"ticker": ["A"] * 4,
                          "date": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
                          "close": pd.Series([100, None, 110, 120], dtype="Float64")})
    path = tmp_path / "prices.parquet"
    frame.to_parquet(path)
    monkeypatch.setattr(store, "PRICES_FILE", path)
    monkeypatch.setattr(store, "US_PRICES_FILE", tmp_path / "missing.parquet")
    prices = store.load_all_dated_closes()
    assert prices["A"][1] == [100.0, None, 110.0, 120.0]
    result = hypo_score.score([{"as_of": "2026-09-01", "tickers": ["A"]}], prices, horizon=1)
    assert result["matured"] == 0  # Never slide the entry to the next available price.
    assert result["lift_pp"] is None


def test_api_hides_technical_exception_but_discloses_verification_failure(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api.hypothesis, "get", lambda **kw: {"ready": True})
    monkeypatch.setattr(api.db, "hypo_runs_recent", lambda *args: [])
    def fail():
        raise TypeError("sensitive internal details")
    monkeypatch.setattr(api.store, "load_all_dated_closes", fail)
    out = api.hypothesis_get()
    assert out["accuracy"]["status"] == "unavailable"
    assert "성과는 확인할 수 없습니다" in out["accuracy"]["blocked_reason"]
    assert "TypeError" not in json.dumps(out) and "sensitive" not in json.dumps(out)


def test_dividend_calendar_only_accepts_explicit_payment_schedule():
    html = HTML_PATH.read_text()
    fn = html[html.index("function dividendPlanSummary("):html.index("function recalcDividends(")]
    js = fn + """
const items = [
 {ticker:'unknown',dps:12,price:100,div_months:[]},
 {ticker:'fiscal',dps:24,price:100,div_months:[3,6,9,12]},
 {ticker:'known',dps:36,price:100,payment_months:[4],payment_schedule_status:'confirmed'},
];
console.log(JSON.stringify({
 mixed:dividendPlanSummary(items,{unknown:1,fiscal:1,known:1}),
 empty:dividendPlanSummary(items,{}),
 missing:dividendPlanSummary(items,{missing:1}),
 bad:dividendPlanSummary([{ticker:'bad',dps:12,price:null,payment_months:[0,13],payment_schedule_status:'confirmed'}],{bad:1})
}));
"""
    result = subprocess.run(["node"], input=js, text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert data["mixed"]["months"] == [0, 0, 0, 36, 0, 0, 0, 0, 0, 0, 0, 0]
    assert data["mixed"]["unscheduledAnnual"] == 36
    assert data["mixed"]["annual"] == 72
    assert data["empty"]["annual"] == 0
    assert data["missing"]["missingTickers"] == ["missing"]
    assert data["bad"]["unscheduledAnnual"] == 12
    assert data["bad"]["cost"] is None


def test_reference_views_do_not_claim_predictions_or_live_weights():
    html = HTML_PATH.read_text()
    issue = html[html.index("function _hypoToEcharts("):html.index("async function loadHypothesis(")]
    assert "support_pct" not in issue and "branch_pct" not in issue
    assert "12개월 모두 배당 유입" not in html
    assert "종목 추가로 채워보세요" not in html
    assert "경기 사이클(확정)" not in html
    assert "설명용 그림" in html
    etf = html[html.index("async function loadEtfs("):html.index("async function loadGurus(")]
    assert "e.holdings" not in etf and "echarts.init" not in etf
    assert "최신 구성비는 제공하지 않습니다" in etf
    assert "공시일 미확인" in html and "수집일 미확인" in html


def test_dividend_failed_reload_clears_previous_market_amounts():
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const html = fs.readFileSync('src/signal_desk/web/index.html','utf8');
const elements = {};
for (const m of html.matchAll(/id="(div-[^"]+)"/g)) elements[m[1]]={style:{},textContent:'',innerHTML:'',value:''};
const values = {sd_divplan_us:'{"A":1}',sd_divplan_kr:'{"B":1}'};
let calls=[];
const chart={setOption(){},resize(){},clear(){}};
const ctx=vm.createContext({document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},
 localStorage:{getItem:key=>values[key],setItem:(k,v)=>values[k]=v},
 fmtNum:(v,dp)=>Number(v).toFixed(dp),fmtKRW:v=>String(v),esc:v=>String(v),kindLabel:v=>v,
 echarts:{init:()=>chart},fetch:()=>new Promise(resolve=>calls.push(resolve))});
vm.runInContext(html.slice(html.indexOf('let _divItems ='),html.indexOf('// ===== 포트폴리오 파이차트')),ctx);
const run=code=>vm.runInContext(code,ctx);
(async()=>{
 const us=run('loadDividends()');
 run("_divMarket='kr'"); const kr=run('loadDividends()');
 calls[1]({ok:true,json:async()=>({ready:true,currency:'KRW',items:[{ticker:'B',dps:12,price:100}]})}); await kr;
 assert.equal(elements['div-annual'].textContent,'12');
 calls[0]({ok:true,json:async()=>({ready:true,currency:'USD',items:[{ticker:'A',dps:999,price:100}]})}); await us;
 assert.equal(elements['div-annual'].textContent,'12');
 const failed=run('loadDividends()');
 assert.equal(elements['div-annual'].textContent,'확인 필요');
 calls[2]({ok:false}); await failed;
 assert.equal(elements['div-annual'].textContent,'확인 필요');
 assert.equal(elements['div-calendar'].style.display,'none');
 assert.equal(run('_divItems.length'),0);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    out = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=15)
    assert out.returncode == 0, out.stdout + out.stderr
