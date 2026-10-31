"""List thumbnails use stored closes without silently certifying current prices."""
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from signal_desk import api


def test_preview_is_read_only_and_skips_score_and_indicator_paths(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not calculate scores or indicators")
    rows = [{"date": f"2026-10-{i:02}", "close": i * 100} for i in range(1, 32)]
    rows[15]["close"] = float("nan")
    monkeypatch.setattr(api.store, "load_price_history", lambda ticker: rows)
    monkeypatch.setattr(api.store, "load_us_price_history", lambda ticker: rows[-3:])
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-30")
    monkeypatch.setattr(api, "compute_indicator_series", forbidden)
    monkeypatch.setattr(api, "chart_scores_and_zones", forbidden)
    data = api.signal_chart_get("005930", preview=True, flow=True)
    assert data["dates"] == [f"2026-10-{i:02}" for i in range(11, 31)]
    assert data["close"][5] is None
    assert data["as_of"] == data["expected_as_of"] == "2026-10-30"
    assert data["reference_only"] and data["basis"] == "stored_close"
    assert api.signal_chart_get("AAPL", market="us", preview=True)["dates"] == ["2026-10-29", "2026-10-30"]
    monkeypatch.setattr(api.store, "load_price_history", lambda ticker: [])
    assert not api.signal_chart_get("NONE", preview=True)["ready"]
    for path in ("/mini-charts.js", "/mini-charts.css"):
        response = TestClient(api.app).get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_actual_geometry_loading_limits_gaps_and_workspace_switching():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real UI module")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const observed=new Set(),requests=[],els={},buttons=[],tick=()=>new Promise(setImmediate);
let notify,focus=0;
const element=(id,ticker)=>({id,dataset:{miniTicker:ticker,miniMarket:'kospi',miniClose:'2026-10-30'},
  isConnected:true,innerHTML:'',textContent:'',hiddenClasses:new Set(),
  classList:{toggle(name,on){els[id].hiddenClasses[on?'add':'delete'](name)}},
  setAttribute(k,v){this[k]=v},focus(){focus++},getBoundingClientRect:()=>({top:20,bottom:80})});
const el=id=>els[id] ||= element(id,id);
const ctx=vm.createContext({window:{},console,Date,Number,String,Math,Array,Object,Promise,AbortController,
  localStorage:{getItem(){throw Error('unavailable')},setItem(){throw Error('unavailable')}},
  setTimeout,clearTimeout,innerHeight:800,
  document:{getElementById:el,querySelectorAll:()=>buttons},renderSignalList(){},
  IntersectionObserver:class {constructor(fn){notify=fn} observe(e){observed.add(e)} unobserve(e){observed.delete(e)}},
  fetch:url=>new Promise(resolve=>requests.push({url,resolve}))});
vm.runInContext(fs.readFileSync('src/signal_desk/web/mini-charts.js','utf8'),ctx);
const mini=ctx.window.MiniCharts;
const data={ready:true,reference_only:true,basis:'stored_close',dates:['2026-10-28','2026-10-29','2026-10-30'],
  close:[100,null,110],as_of:'2026-10-30',expected_as_of:'2026-10-30'};
const geometry=mini.geometry(data,'2026-10-30');
assert.equal((geometry.path.match(/M/g)||[]).length,2);assert.doesNotMatch(geometry.path,/L/);
assert.equal(geometry.missing,true);assert.match(geometry.label,/결측/);
assert.equal(mini.geometry({...data,close:[100,100,100]},'2026-10-30').path,'M3.00 23.00 L70.00 23.00 L137.00 23.00');
assert.equal(mini.geometry({...data,expected_as_of:'2026-10-29'}),null);
assert.equal(mini.geometry({...data,dates:['2026-02-30','2026-10-29','2026-10-30']}),null);
assert.equal(mini.geometry({...data,close:[null,NaN,110]}),null);
assert.equal(mini.geometry({...data,close:[1,2]}),null);
assert.equal(mini.geometry({...data,reference_only:false}),null);
assert.match(mini.geometry({...data,expected_as_of:'2026-11-02'}).label,/기준 확인 필요/);
assert.match(mini.geometry(data,'2026-10-29').label,/기준 확인 필요/);
assert.doesNotMatch(mini.markup('<img>','us','2026-10-30'),/<img>/);
(async()=>{
 const a=el('A'),same=el('A2'),b=el('B'),c=el('C');same.dataset.miniTicker='A';
 mini.scan({querySelectorAll:()=>[a,same,b,c]});
 assert.equal(requests.length,0); // No hidden bulk fetch.
 notify([a,same,b,c].map(target=>({target,isIntersecting:true})));
 assert.equal(requests.length,1); // Shared A request, two active consumers.
 requests[0].resolve({ok:true,json:async()=>data});await tick();await tick();
 assert.equal(requests.length,3); // B and C, at most two network requests together.
 b.isConnected=false;
 requests[1].resolve({ok:true,json:async()=>data});requests[2].resolve({ok:false});await tick();await tick();
 assert.equal(b.innerHTML,'');assert.match(a.innerHTML,/<svg/);assert.match(c.innerHTML,/확인 불가/);
 const cached=el('cached');cached.dataset.miniTicker='A';
 mini.scan({querySelectorAll:()=>[cached]});notify([{target:cached,isIntersecting:true}]);await tick();
 assert.equal(requests.length,3);assert.match(cached.innerHTML,/<svg/);
 mini.toggle();assert.equal(mini.enabled(),false);mini.toggle();assert.equal(mini.enabled(),true);
 vm.runInContext(fs.readFileSync('src/signal_desk/web/investment.js','utf8'),ctx);
 focus=0;
 const guide=ctx.window.InvestmentGuide;
 for(const view of ['account','analysis']){const button=el(view);button.dataset.investmentView=view;buttons.push(button)}
 guide.showWorkspace('account');
 assert.equal(el('manual-holdings-card').hiddenClasses.has('investment-view-hidden'),true);
 assert.equal(el('real-holdings-card').hiddenClasses.has('investment-view-hidden'),false);
 // A late async inline display update cannot override the class's !important CSS.
 guide.showWorkspace('analysis',true);assert.equal(focus,1);
 assert.equal(el('real-holdings-card').hiddenClasses.has('investment-view-hidden'),true);
 assert.equal(el('pf-tools').hiddenClasses.has('investment-view-hidden'),false);
 assert.equal(el('investment-dividend-workspace').hiddenClasses.has('investment-view-hidden'),false);
 assert.match(el('investment-workspace-note').textContent,/자동으로 맞춰지지/);
 assert.equal(buttons[1]['aria-pressed'],'true');assert.equal(requests.length,3);
 let resized=0,resizeFrame;
 ctx.window.requestAnimationFrame=fn=>{resizeFrame=fn};
 ctx.window.echarts={getInstanceByDom:()=>({resize(){resized++}})};
 ctx.document.querySelectorAll=selector=>selector.startsWith('#trading-rebal')
   ?[{getBoundingClientRect:()=>({width:0,height:210})},{getBoundingClientRect:()=>({width:600,height:210})}]:buttons;
 guide.showWorkspace('account');resizeFrame();assert.equal(resized,1);
 assert.equal(el('investment-dividend-workspace').hiddenClasses.has('investment-view-hidden'),true);
 const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
 assert.ok(html.indexOf('id="bot-equity-chart"')<html.indexOf('id="bot-trades-body"'));
 assert.match(html,/showWorkspace\(seg==='live'\?'account':seg==='dividend'\?'analysis'/);
 assert.match(html,/switchTab\('trading'\);\s+InvestmentGuide.showWorkspace\('analysis'\)/);
 const review=guide.renderReview({seed_cash:1000,total_eval:800,pnl:-30,currency:'KRW',positions:[]});
 assert.ok(review.indexOf('계좌 전체 손익')<review.indexOf('<details'));
 assert.match(review,/보유 중인 손익/);assert.match(review,/<summary>손익 계산과 다음 점검 계획/);
})().catch(error=>{console.error(error);process.exit(1)});
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
