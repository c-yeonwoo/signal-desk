"""The candidate-first view must not invent candidates or certify missing controls."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOT = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8'),els={},pending=[],intervals=[];
const el=id=>els[id] ||= {textContent:'',innerHTML:'',hidden:false,dataset:{},style:{display:''},
  querySelectorAll:()=>[],appendChild(){},focus(){}};
const ctx=vm.createContext({console,Date,Number,Math,Object,Array,String,Promise,Error,AbortController,
  document:{getElementById:el,addEventListener(){},visibilityState:'visible'},
  window:{},_sigMarket:'kospi',_sigCache:{},setInterval(fn){intervals.push(fn)},setTimeout(){return 1},clearTimeout(){},
  _isBuy:kind=>['BUY','STRONG_BUY'].includes(kind),kindLabel:kind=>kind,
  fmtNum:(v,d)=>Number(v).toFixed(d),gotoSignal(){},
  fetch:url=>new Promise(resolve=>pending.push({url,resolve}))});
vm.runInContext(html.slice(html.indexOf('const _SIGNAL_KIND_ORDER ='),html.indexOf('function renderSignalList(){')),ctx);
vm.runInContext(fs.readFileSync('src/signal_desk/web/today-desk.js','utf8'),ctx);
const desk=ctx.window.TodayDesk,now=Date.now()/1000;
const row=(ticker,extra={})=>({ticker,name:ticker,kind:'BUY',price:100,score:1.5,
  current_decision_eligible:true,current_decision_status:'eligible',signal_price_status:'current_quote',
  signal_quote_received_at:now,price_validity:{valid_until:now+600},
  score_contributions:{momentum:.5,quality:.2,technical:0,flow:-.1},...extra});
const data=items=>({ready:true,items});
const reply=(index,value,ok=true)=>pending[index].resolve({ok,json:async()=>value});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
"""


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the actual candidate renderer")
    result = subprocess.run([node, "-e", BOOT + script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_candidates_keep_existing_final_order_and_never_backfill_blocked_slots():
    run(r"""
const rows=[row('H',{kind:'HOLD',score:3}),row('B',{score:2}),
  row('A',{kind:'STRONG_BUY',score:1.5}),row('G',{gate_blocked:true,score:3}),
  row('D',{kind:'DEFERRED',reference_kind:'STRONG_BUY'}),
  row('L',{low_coverage:true}),row('M',{price:null}),
  row('S',{signal_quote_received_at:now-1000}),row('F',{decision_buy_blocked:true})];
const before=JSON.stringify(rows),result=desk.model(data(rows),{buy_paused:false});
assert.deepEqual(Array.from(result.candidates,r=>r.ticker),['A','B']);
assert.equal(JSON.stringify(rows),before);
assert.match(desk.describe(rows[1]).reason,/중기 가격 흐름·재무 건전성/);
assert.match(desk.describe(rows[1]).caution,/외국인·기관 수급/);
for(const key of ['short','quality','momentum','flow']){
  const caution=desk.describe(row('COPY',{score_contributions:{[key]:-.1}})).caution;
  assert.match(caution,/ 항목이 점수를 낮췄습니다\.$/);
  assert.doesNotMatch(caution,/공매도은/);
}
assert.equal(desk.model(data([rows[0],rows[3]]),{buy_paused:false}).candidates.length,0);
""")


def test_close_reference_pause_and_unknown_controls_are_different_states():
    run(r"""
const closed=row('C',{signal_price_status:'current_close',current_decision_eligible:false,
  current_decision_status:'close_reference_only',signal_close_date:'2026-10-30'});
const reference=desk.model(data([closed]),{buy_paused:false});
assert.equal(reference.closeOnly,true);assert.match(reference.title,/지난 장 마감/);
assert.equal(reference.candidates.length,1);assert.equal(desk.describe(closed).live,false);
const paused=desk.model(data([row('A')]),{buy_paused:true});
assert.match(paused.title,/봇의 새 매수.*보류/);assert.equal(paused.candidates.length,1);
assert.match(desk.model(data([row('A')]),null).title,/확인하지 못/);
assert.match(desk.model(data([row('A')]),{}).title,/확인하지 못/);
assert.match(desk.model(data([row('A',{kind:'HOLD'})]),{buy_paused:false}).title,/통과한 후보가 없/);
const unavailable=desk.model({ready:false,items:[row('A')]},{buy_paused:false});
assert.equal(unavailable.candidates.length,0);assert.match(unavailable.title,/분석 자료/);
assert.doesNotMatch(unavailable.title,/통과한 후보가 없/);
""")


def test_open_page_expiry_removes_current_candidates_without_changing_their_grade():
    run(r"""
const expired=row('E',{price_validity:{valid_until:now-1}});
assert.equal(desk.model(data([expired]),{buy_paused:false}).candidates.length,0);
assert.equal(expired.kind,'BUY');assert.match(desk.model(data([expired]),{buy_paused:false}).title,/가격 확인/);
const sourceMissing=row('N',{signal_price_status:'unknown',current_decision_eligible:false});
assert.equal(desk.model(data([sourceMissing]),{buy_paused:false}).candidates.length,0);
""")


def test_slow_control_does_not_block_candidates_and_late_market_response_is_ignored():
    run(r"""
(async()=>{
 const kr=desk.load('kospi');reply(0,data([row('<국내>')]));await flush();
 assert.match(el('today-candidates').innerHTML,/&lt;국내&gt;/);
 assert.match(el('today-status-title').textContent,/확인하지 못/);
 ctx._sigMarket='us';const us=desk.load('us');reply(2,data([row('US')]));
 reply(3,{buy_paused:true});await us;
 reply(1,{buy_paused:false});await kr;
 assert.match(el('today-candidates').innerHTML,/US/);
 assert.doesNotMatch(el('today-candidates').innerHTML,/국내/);
 assert.match(el('today-status-title').textContent,/보류/);
 assert.equal(ctx._sigCache.us,undefined); // Today cannot short-circuit the list's favorites initialization.
 assert.ok(pending.every(x=>x.url.startsWith('/api/signals?')||x.url.startsWith('/api/ledger/state?')));
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_periodic_repaint_keeps_keyboard_focus_and_handles_expired_focused_candidate():
    run(r"""
(async()=>{
 const item=row('A'),request=desk.load('kospi');reply(0,data([item]));reply(1,{buy_paused:false});await request;
 let focused='old';
 ctx.document.activeElement={getAttribute:key=>key==='data-today-stock'?'A':null};
 const newButton={dataset:{todayStock:'A'},addEventListener(){},focus(){focused='A'}};
 el('today-candidates').querySelectorAll=()=>[newButton];intervals[0]();assert.equal(focused,'A');
 item.price_validity.valid_until=now-1;
 el('today-candidates').querySelectorAll=()=>[];
 el('today-status-title').focus=()=>{focused='status'};
 el('today-retry').focus=()=>{focused='retry'};
 intervals[0]();assert.equal(focused,'retry');
 assert.equal(el('today-retry').hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_failed_refresh_labels_cached_candidates_as_history_and_keeps_retry():
    run(r"""
(async()=>{
 ctx._sigCache.kospi={d:data([row('OLD')]),ts:Date.now()-300000};
 const request=desk.load('kospi');reply(0,{},false);reply(1,{},false);await request;
 assert.match(el('today-status-title').textContent,/불러오지 못/);
 assert.match(el('today-candidates').innerHTML,/지난 후보 기록/);
 assert.doesNotMatch(el('today-candidates').innerHTML,/>매수 후보</);
 assert.equal(el('today-retry').hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_initial_failure_does_not_invent_a_previous_record_or_a_normal_empty_result():
    run(r"""
(async()=>{
 const request=desk.load('kospi');reply(0,{},false);reply(1,{buy_paused:false});await request;
 assert.match(el('today-candidate-title').textContent,/조회 실패/);
 assert.doesNotMatch(el('today-status-note').textContent,/마지막.*기록/);
 assert.doesNotMatch(el('today-candidates').innerHTML,/조건을 통과한 종목이 생기면/);
 assert.equal(el('today-retry').hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_candidate_assets_are_served_with_revalidation():
    from fastapi.testclient import TestClient
    from signal_desk import api

    client = TestClient(api.app)
    for path, mime in (("/today-desk.js", "text/javascript"), ("/today-desk.css", "text/css")):
        response = client.get(path)
        assert response.status_code == 200
        assert mime in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"

