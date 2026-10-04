"""Beginner explanations must preserve ledger arithmetic and source uncertainty."""

from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from signal_desk import api


def test_investment_assets():
    client = TestClient(api.app)
    for path in ("/investment.js", "/investment.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_explanations_and_request_isolation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser component tests")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const ctx = vm.createContext({window:{},console});
vm.runInContext(fs.readFileSync('src/signal_desk/web/investment.js','utf8'),ctx);
const guide = ctx.window.InvestmentGuide;
const state = {seed_cash:1000,total_eval:800,pnl:-30,currency:'KRW',positions:[]};
let r = guide.review(state);
assert.equal(r.pnl,-200); assert.equal(r.realized,-170); assert.equal(r.open,-30);
assert.equal(r.recovery,25); assert.equal(r.driver,'realized');
assert.equal(guide.review({...state,unrealized_pnl:10}).realized,-210);
assert.equal(guide.review({...state,pnl:-300}).driver,'open');
assert.equal(guide.review({...state,total_eval:1100}).recovery,0);
assert.equal(guide.review({...state,total_eval:1000,pnl:0}).driver,'flat');
assert.equal(guide.review({...state,pnl:-100}).driver,'mixed');
assert.equal(guide.review({...state,total_eval:0}).recovery,null);
assert.equal(guide.review({...state,total_eval:-1}).recovery,null);
assert.equal(guide.review({...state,pnl:null}).realized,null);
assert.equal(guide.review({...state,seed_cash:0}).ready,false);
assert.equal(guide.review({...state,total_eval:NaN}).driver,'unknown');
assert.equal(guide.review({...state,pnl:'0'}).driver,'unknown');
assert.equal(guide.review({...state,costs:{total_execution_cost:20}}).pnl,-200);
assert.match(guide.renderReview(state),/\+25.00%/);
assert.match(guide.renderReview(state),/현재는 보유종목이 없습니다/);
assert.doesNotMatch(guide.renderReview({...state,total_eval:1100}),/초기자금까지 남은 거리/);
assert.doesNotMatch(guide.renderReview({...state,pnl:null}),/재진입을 늦추거나/);
assert.doesNotMatch(guide.renderReview({...state,label:'<img src=x>'}),/<img/);
const g = {kind:'concentration',name:'<script>x</script>',current_pct:40,limit_pct:15,priority:'high'};
const portfolio = guide.renderPortfolio({data_quality:{status:'partial'},guidance:[g],summary:{cash:0},currency:'USD'});
assert.match(portfolio,/자료가 일부 부족/); assert.match(portfolio,/40.0%/); assert.match(portfolio,/15.0%/);
assert.match(portfolio,/\$0.00/); assert.doesNotMatch(portfolio,/<script>/);
assert.doesNotMatch(portfolio,/아래 비중 조정은/);
assert.match(guide.renderPortfolio({data_quality:{status:'complete'},allocation:{ready:true}}),/주문으로 이어지지 않아요/);
assert.match(guide.explain({kind:'correlation',tickers:['A'],current_pct:50,limit_pct:40},[{ticker:'A',name:'회사이름'}])[1],/회사이름/);
assert.match(guide.explain({kind:'monitor'})[1],/손실 위험이 없거나/);
assert.match(guide.explain({kind:'future',action:'새 규칙'})[0],/새 규칙/);
assert.match(guide.renderCosts({costs:{trades:20,cost_recorded_trades:2,total_execution_cost:30}}),/전체 비용은 알 수 없습니다/);
assert.match(guide.renderCosts(null),/판단할 수 없습니다/);
assert.match(guide.renderCosts({currency:'USD',costs:{trades:0,cost_recorded_trades:0,total_execution_cost:0}}),/\$0.00/);
const html = fs.readFileSync('src/signal_desk/web/index.html','utf8');
// Parse every actual inline script, not a copied implementation.
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const elements = {};
function el(id) {return elements[id] ||= {textContent:'',innerHTML:'',className:'',attributes:{},
  setAttribute(k,v){this.attributes[k]=v},previousElementSibling:{},parentElement:{},
  classList:{add(){},remove(){},toggle(){}}};}
const pending = [];
const watchdog = setTimeout(()=>{console.error('Unresolved UI request');process.exit(1)},10000);
Object.assign(ctx,{InvestmentGuide:guide,document:{getElementById:el,querySelectorAll:()=>[]},
  getJSON:url=>new Promise(resolve=>pending.push({url,resolve})),esc:String,
  fmtNum:(v)=>String(v),fmtKRW:v=>String(v),
  echarts:{init:()=>({setOption(){},resize(){},clear(){},dispose(){}})}});
vm.runInContext(html.slice(html.indexOf('let _botChart = null;'),html.indexOf('// ===== 트레이딩 세그먼트')),ctx);
function answer(i,data,ok=true) {pending[i].resolve({ok,data,reason:ok?'':'failed'});}
const base = {...state,label:'균형형',positions:[],recent_trades:[],config:{},risk_policy:{}};
(async()=>{
  const old = vm.runInContext('loadBotState()',ctx);
  vm.runInContext("_botMarket='us'; _ledgerStyle='aggressive'",ctx);
  const latest = vm.runInContext('loadBotState()',ctx);
  answer(1,{...base,currency:'USD',label:'공격형'});
  await new Promise(setImmediate);
  assert.equal(pending[2].url,'/api/reference-performance?market=us');
  answer(2,{currency:'USD',bots:[{style:'aggressive',curve:[],days:3}]});
  answer(3,{currency:'USD',costs:{trades:0,cost_recorded_trades:0,total_execution_cost:0}});
  await new Promise(setImmediate);
  assert.equal(pending[4].url,'/api/verdict');
  answer(4,{status:'pending'});
  await latest;
  answer(0,base); await old;
  assert.match(el('bot-review').innerHTML,/공격형/);
  assert.match(el('bot-perf-summary').innerHTML,/따라가기 닫힘/);
  assert.equal(pending.length,5); // ledger, performance, costs, one verdict; stale state adds none.
  assert.equal(el('portfolio-bot').attributes['aria-busy'],'false');
  // A performance response from an old style must also be ignored.
  // Verdict is already cached, so later loads do not ask again.
  const p1 = vm.runInContext('loadBotState()',ctx); answer(5,base);
  await new Promise(setImmediate);
  const p2 = vm.runInContext('loadBotState()',ctx); answer(8,{...base,label:'최신'});
  await new Promise(setImmediate);
  answer(9,{bots:[{style:'aggressive',label:'최신',curve:[],days:3}]}); answer(10,null,false); await p2;
  answer(6,{bots:[{style:'aggressive',label:'오래된 응답',curve:[],days:3}]});
  answer(7,{costs:{trades:999,cost_recorded_trades:999,total_execution_cost:999}}); await p1;
  assert.doesNotMatch(el('reference-perf').innerHTML,/오래된 응답/);
  assert.doesNotMatch(el('bot-cost-review').innerHTML,/999/);
  assert.equal(pending.length,11);
  const failed = vm.runInContext('loadBotState()',ctx); answer(11,null,false); await failed;
  assert.match(el('bot-review').innerHTML,/다시 확인/);
  assert.equal(el('portfolio-bot').attributes['aria-busy'],'false');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>clearTimeout(watchdog));
"""
    out = subprocess.run([node], input=script, text=True, capture_output=True,
                         cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert out.returncode == 0, out.stdout + out.stderr
