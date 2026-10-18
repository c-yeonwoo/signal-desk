"""PD3: actual UI code must not confuse failed reads with empty accounts."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_account_and_input_reads_are_isolated_and_fail_closed():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for actual UI contracts")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8'),elements={};
const el=id=>elements[id] ||= {innerHTML:'',textContent:'',style:{},value:'',disabled:false,open:false,dataset:{},
  setAttribute(k,v){this[k]=v},querySelector(){return {focus(){}}},scrollIntoView(){}};
const pending=[];
const ctx=vm.createContext({window:{},console,Date,Map,Number,document:{getElementById:el},
  fetch:url=>new Promise(resolve=>pending.push({url,resolve})),esc:v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  fmtNum:(v,n=0)=>Number(v).toFixed(n),_clearWeightPreview(){},loadAccountPerformance(){}});
vm.runInContext(fs.readFileSync('src/signal_desk/web/investment.js','utf8'),ctx);
ctx.InvestmentGuide=ctx.window.InvestmentGuide;
vm.runInContext(html.slice(html.indexOf("let _pfMarket = 'kr';"),html.indexOf('function setPfMarket(')),ctx);
vm.runInContext(html.slice(html.indexOf('let _realHoldingsImportFingerprint = null;'),html.indexOf('async function loadAccountPerformance(')),ctx);
vm.runInContext(html.slice(html.indexOf('async function loadHoldings(){'),html.indexOf('// 내 보유 배당(')),ctx);
vm.runInContext(html.slice(html.indexOf('async function loadPortfolioProfile(){'),html.indexOf('function _piPriority(')),ctx);
for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(m[1]);
const answer=(i,d,status=200)=>pending[i].resolve({ok:status>=200&&status<300,status,json:async()=>d});
const tick=()=>new Promise(setImmediate);
const us={symbol:'AAPL',name:'Apple',marketCountry:'US',quantity:'2',averagePurchasePrice:'200',lastPrice:null,marketValue:{amount:null}};
const kr={symbol:'005930',name:'삼성전자',marketCountry:'KR',quantity:'2',averagePurchasePrice:'100',lastPrice:'110',marketValue:{amount:'220'}};
const manual=[{ticker:'005930',qty:2,avg_price:100},{ticker:'AAPL',qty:2,avg_price:200}];
(async()=>{
  // Start KR, then US. Old KR broker/manual results cannot paint a US account.
  const old=ctx.loadRealHoldings();
  vm.runInContext("_pfMarket='us'",ctx); const latest=ctx.loadRealHoldings();
  assert.deepEqual(pending.map(p=>p.url),['/api/holdings','/api/my-holdings','/api/holdings','/api/my-holdings']);
  answer(2,{holdings:manual});answer(3,{ready:true,items:[kr,us],import_fingerprint:'latest'});await latest;
  assert.match(el('holdings-body').innerHTML,/Apple|AAPL/);assert.doesNotMatch(el('holdings-body').innerHTML,/005930/);
  assert.match(el('real-holdings-body').innerHTML,/확인 필요/);assert.match(el('real-holdings-sum').innerHTML,/평가액 확인 필요/);
  const body=el('real-holdings-body').innerHTML;
  answer(0,{holdings:[]});answer(1,{ready:true,items:[kr],import_fingerprint:'old'});await old;
  assert.equal(el('real-holdings-body').innerHTML,body);
  assert.equal(vm.runInContext('_realHoldingsImportFingerprint',ctx),'latest');
  // HTTP failure cannot masquerade as ready/empty. Retry resets old import evidence.
  const failed=ctx.loadRealHoldings();answer(4,{holdings:manual});answer(5,{ready:true,items:[]},500);await failed;
  assert.match(el('real-holdings-body').innerHTML,/보유종목이 없다는 뜻은 아닙니다/);
  assert.equal(vm.runInContext('_realHoldingsImportFingerprint',ctx),null);
  assert.equal(el('real-holdings-refresh').disabled,false);
  assert.equal(el('real-holdings-note').style.display,'none');
  const empty=ctx.loadRealHoldings();answer(6,{holdings:[]});answer(7,{ready:true,items:[]});await empty;
  assert.match(el('real-holdings-body').innerHTML,/보유 종목이 없어요/);assert.equal(el('holdings-editor').open,true);
  // Failed manual read does not become an empty comparison allowing full replacement.
  const badInput=ctx.loadRealHoldings();answer(8,{holdings:[]},503);answer(9,{ready:true,items:[us],import_fingerprint:'unsafe'});await badInput;
  assert.match(el('holdings-summary').innerHTML,/저장 내역은 지우지 않았습니다/);
  assert.equal(vm.runInContext('_realHoldingsImportFingerprint',ctx),null);
  assert.equal(el('portfolio-analyze').disabled,true);
  assert.match(el('real-holdings-body').innerHTML,/Apple/);
  const forbidden=ctx.loadRealHoldings();answer(10,{holdings:manual});answer(11,{forbidden:true},403);await forbidden;await tick();
  assert.equal(el('real-holdings-card').style.display,'none');
  assert.match(el('holdings-body').innerHTML,/AAPL/);
  // Same-market retries: earlier input read and broker error cannot erase the newer success.
  const first=ctx.loadRealHoldings(),second=ctx.loadRealHoldings();
  answer(14,{holdings:manual});answer(15,{ready:true,items:[us]});await second;
  answer(12,{holdings:[]});answer(13,{ready:false,reason:'old error'});await first;
  assert.match(el('real-holdings-body').innerHTML,/Apple/);assert.match(el('holdings-summary').textContent,/1종목/);
  // Cash/limits from another market, or a failed request, must not become editable zero defaults.
  vm.runInContext("_pfMarket='kr'",ctx);const p1=ctx.loadPortfolioProfile();
  vm.runInContext("_pfMarket='us'",ctx);const p2=ctx.loadPortfolioProfile();
  answer(17,{configured:true,cash:123,max_single_position_pct:15,max_sector_pct:35,max_cluster_pct:45,min_cash_pct:5});await p2;
  answer(16,{cash:999});await p1;assert.equal(el('pi-cash').value,123);
  const p3=ctx.loadPortfolioProfile();answer(18,{cash:0},503);await p3;
  assert.equal(el('pi-cash').value,'');assert.equal(el('portfolio-analyze').disabled,true);
  assert.match(el('pi-profile-note').textContent,/진단을 보류/);
  assert.match(el('pi-result').innerHTML,/설정 다시 확인/);
  const p4=ctx.loadPortfolioProfile();answer(19,{});await p4;
  assert.equal(el('pi-cash').value,'');assert.equal(el('portfolio-analyze').disabled,true);
  vm.runInContext(html.slice(html.indexOf('async function loadAccountPerformance('),html.indexOf('async function importRealHoldings(){')),ctx);
  const perf1=ctx.loadAccountPerformance('us'),perf2=ctx.loadAccountPerformance('us');
  const point={date:'2026-10-16',observed_at:1792152000,holdings_value:123,daily_pnl:3,daily_return_pct:2.5};
  answer(21,{points:[point]});await perf2;
  answer(20,{points:[{...point,holdings_value:999}]});await perf1;
  assert.match(el('account-performance-summary').innerHTML,/123/);
  assert.doesNotMatch(el('account-performance-summary').innerHTML,/999/);
  const perf3=ctx.loadAccountPerformance('us'),refresh=ctx.loadRealHoldings();
  answer(23,{holdings:manual});answer(24,{ready:false},503);await refresh;
  answer(22,{points:[{...point,holdings_value:888}]});await perf3;
  assert.doesNotMatch(el('account-performance-summary').innerHTML,/888/);
  assert(pending.every(p=>!p.url.includes('import')&&!p.url.includes('submit')));
  const unsafe=ctx.InvestmentGuide.renderHoldingInputs([{ticker:"A'X",label:'<img src=x>',qty:null,avg_price:null}],'us');
  assert.doesNotMatch(unsafe,/<img/);assert.match(unsafe,/확인 필요/);assert.match(unsafe,/this.dataset.removeHolding/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True, cwd=ROOT, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_path_is_static_and_history_is_optional():
    html = (ROOT / "src/signal_desk/web/index.html").read_text()
    pane = html.split('id="trading-rebal"', 1)[1].split('id="trading-dividend"', 1)[0]
    order = ['id="pf-market-seg"', 'id="real-holdings-card"', 'id="investment-account-workspace"',
             'id="manual-holdings-card"', 'id="portfolio-intelligence-card"', 'id="pf-history"']
    assert [pane.index(s) for s in order] == sorted(pane.index(s) for s in order)
    assert 'id="pf-history" class="pf-advanced" open' not in pane
    assert 'id="holdings-editor" class="pf-settings" open' not in pane
    assert 'id="investment-account-mount"' in pane and 'id="investment-dividend-mount"' in pane
    view = html.split('async function loadRealHoldings(){', 1)[1].split('async function loadAccountPerformance(', 1)[0]
    assert 'insertBefore' not in view
    assert '/api/my-holdings/import' not in view
