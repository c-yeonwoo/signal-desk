"""종목 상세의 비동기 최신 요청·가격 결측·기존 차트 기능 계약을 실제 JS로 검증한다."""

from pathlib import Path
import shutil
import subprocess

import pytest


def _node(script):
    if not shutil.which("node"):
        pytest.skip("Node is needed for the stock workspace")
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


_SOURCE = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
function source(start,end){const i=html.indexOf(start);assert.ok(i>=0,start);return html.slice(i,html.indexOf(end,i));}
"""


def test_missing_or_nonpositive_price_never_formats_as_a_tradable_quote():
    _node(_SOURCE + r"""
const ctx=vm.createContext({Date,Number,_sigMarket:'kospi',fmtNum:v=>String(v)});
vm.runInContext(source('function signalPriceView(', 'function renderSignalList('),ctx);
for(const price of [null,undefined,0,-1,NaN,Infinity,'']){
  assert.equal(ctx.hasSignalPrice({price}),false);
  assert.equal(ctx.signalPriceText({price}),'–');
}
assert.equal(ctx.signalPriceText({price:120}),'120원');
ctx._sigMarket='us';assert.equal(ctx.signalPriceText({price:64.01}),'$64.01');
const old={kind:'STRONG_BUY',price:120,signal_price_status:'stale_close'};
assert.equal(ctx.signalPriceView(old).stale,true);
assert.equal(old.kind,'STRONG_BUY');
""")


def test_list_marks_old_grade_and_missing_price_without_changing_engine_decisions():
    _node(_SOURCE + r"""
const ids={'signal-body':{style:{},innerHTML:'',querySelectorAll:()=>[]},
  'sig-search':{value:''},'sig-sort':{value:'signal'}};
const rows=[
  {ticker:'H',name:'고점수 관망',score:3,kind:'HOLD',price:500,signal_price_status:'current_close'},
  {ticker:'M',name:'가격 결측',score:1.8,kind:'BUY',price:null,signal_price_status:'current_close'},
  {ticker:'S',name:'지연된 판정',score:1.9,kind:'STRONG_BUY',price:120,signal_price_status:'stale_close'}];
const ctx=vm.createContext({Date,Number,Math,Object,String,Infinity,Set,
  document:{activeElement:null,getElementById:id=>ids[id]},_signalRows:rows,_selectedTicker:null,
  _sigMarket:'kospi',_favorites:new Set(),_oppFilter:null,_precisionBucket:null,_lensSnapshot:null,
  _sigTodayMeta:null,_PR_FACTOR:{},_HOLD_TAG_TIP:{},
  readScreen:()=>({}),screenPass:()=>true,updatePrecisionStrip:()=>{},renderOppFilters:()=>{},
  updateScreenUI:()=>{},renderSignalLensPanel:()=>{},syncSignalLayout:()=>{},paintLiveQuotePrices:()=>{},
  _isBuy:kind=>kind==='BUY'||kind==='STRONG_BUY',sigPillTitle:()=>'',
  kindLabel:kind=>({STRONG_BUY:'강력매수',BUY:'매수',HOLD:'관망'}[kind]),
  fmtNum:(v,d)=>Number(v).toFixed(d),esc:v=>String(v)});
vm.runInContext(source('const _SIGNAL_KIND_ORDER =', '// 좁은 카드의 보조 표'),ctx);
assert.equal(ctx.renderSignalList(),'S');
const markup=ids['signal-body'].innerHTML;
const stale=markup.slice(markup.indexOf('data-ticker="S"'),markup.indexOf('data-ticker="M"'));
const missing=markup.slice(markup.indexOf('data-ticker="M"'),markup.indexOf('data-ticker="H"'));
assert.match(stale,/지난 강력매수/);assert.match(stale,/종가 지연/);
assert.match(missing,/현재 판정 보류/);assert.match(missing,/가격 확인 필요/);
assert.doesNotMatch(missing,/>0원</);assert.doesNotMatch(missing,/sig-pill[^>]*>매수</);
assert.deepEqual(rows.map(r=>r.kind),['HOLD','BUY','STRONG_BUY']);
""")


_ASYNC = _SOURCE + r"""
const ids=Object.fromEntries(['signal-chart','signal-whynow','signal-company-review'].map(id=>[id,
  {style:{},textContent:'',innerHTML:'',classList:{remove(){}}}]));
const pending=[],drawn=[],painted=[],reviews=[];
const ctx=vm.createContext({Promise,Error,Object,AbortController,encodeURIComponent,
  document:{getElementById:id=>ids[id]},_selectedTicker:'AAA',_sigMarket:'kospi',
  _chartMode:'simple',INDEX_TICKER:'INDEX',_chartCache:null,_signalItems:{},_activeDetailTab:'metrics',
  _paintSignalDetail:(ticker,item)=>painted.push(item),_paintSignalReview:(ticker,data)=>reviews.push(data),
  markLazyDetail:()=>{},_disposeSignalChart:()=>{},_paintChartAsof:()=>{},
  _drawSignalChart:(ticker,item,data)=>drawn.push(data),_ensureChartFlow:()=>{},
  _chartUrl:ticker=>'/chart/'+ticker,
  fetch:url=>new Promise(resolve=>pending.push({url,resolve}))});
vm.runInContext(source('let _chartAbort = null;', '/** 종가 차트'),ctx);
function reply(url,index,payload,ok=true){
  const request=pending.filter(p=>p.url===url)[index];assert.ok(request,url);
  request.resolve({ok,json:async()=>payload});
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
"""


def test_same_ticker_retry_rejects_late_old_chart_and_detail_even_if_abort_is_ignored():
    _node(_ASYNC + r"""
(async()=>{
  const old=ctx.showSignalChart('AAA',{kind:'HOLD',version:'old'});
  const latest=ctx.showSignalChart('AAA',{kind:'HOLD',version:'new'});
  reply('/chart/AAA',1,{ready:true,version:'new'});
  reply('/api/signals/AAA/detail?market=kospi',1,{ready:true,item:{version:'new-detail'}});
  await latest;
  reply('/chart/AAA',0,{ready:true,version:'old'});
  reply('/api/signals/AAA/detail?market=kospi',0,{ready:true,item:{version:'old-detail'}});
  await old;await flush();
  assert.equal(drawn.length,1);assert.equal(drawn[0].version,'new');
  assert.equal(ctx._signalItems.AAA.version,'new-detail');
  assert.ok(!painted.some(item=>item?.version==='old-detail'));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_failed_chart_is_retryable_and_detail_failure_does_not_erase_a_good_chart():
    _node(_ASYNC + r"""
(async()=>{
  const failed=ctx.showSignalChart('AAA',{kind:'HOLD'});
  reply('/chart/AAA',0,null,false);
  reply('/api/signals/AAA/detail?market=kospi',0,null,false);
  await failed;
  assert.match(ids['signal-chart'].innerHTML,/retrySignalDetail/);
  assert.equal(ctx._chartCache,null);
  const retry=ctx.showSignalChart('AAA',{kind:'HOLD'});
  reply('/chart/AAA',1,{ready:true,version:'retry'});
  reply('/api/signals/AAA/detail?market=kospi',1,null,false);
  await retry;
  assert.equal(drawn.length,1);
  assert.equal(ctx._chartCache.d.version,'retry');
  assert.match(ids['signal-company-review'].innerHTML,/근거가 없다는 뜻은 아닙니다/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_all_chart_modes_keep_price_score_zones_and_trade_markers():
    _node(_SOURCE + r"""
const ids={'signal-chart':{style:{}},'signal-legend':{style:{},innerHTML:''}};
let option;
const ctx=vm.createContext({Math,Number,document:{getElementById:id=>ids[id],documentElement:{}},
  _chartMode:'simple',_sigMarket:'kospi',INDEX_TICKER:'INDEX',
  _ZONE_COLOR:{BUY:'#088151',SELL:'#CB2F2A'},
  _isBuy:kind=>kind==='BUY',kindLabel:kind=>kind,reasonKo:()=>'',esc:v=>String(v),
  fmtNum:(v,d)=>Number(v).toFixed(d),getComputedStyle:()=>({getPropertyValue:()=>''}),
  _ensureSignalChart:()=>({setOption:o=>option=o}),_syncChartModeButtons:()=>{},
  _resizeWhenLaidOut:()=>{}});
vm.runInContext(source('function _drawSignalChart(', '// **`resize()`'),ctx);
const d={dates:['2026-10-01','2026-10-02'],close:[100,90],scores:[1.2,-1.3],
  ma20:[101,95],ma60:[102,96],ma120:[103,97],rsi:[40,30],macd:[1,-1],
  macd_signal:[0,0],macd_hist:[1,-1],flow_foreign:[1,-1],flow_inst:[-2,2],
  actual_dates:['2026-10-01'],zones:[
    {kind:'BUY',start:'2026-10-01',end:'2026-10-01',score:1.2,actual:true},
    {kind:'SELL',start:'2026-10-02',end:'2026-10-02',score:-1.3,actual:false}]};
for(const mode of ['simple','trend','detail']){
  ctx._chartMode=mode;ctx._drawSignalChart('AAA',null,d);
  const by=Object.fromEntries(option.series.map(s=>[s.name,s]));
  assert.deepEqual(by['가격'].data,d.close);
  assert.deepEqual(by['시그널 점수'].data,d.scores);
  assert.equal(by['가격'].markArea.data.length,2);
  assert.equal(by['가격'].markPoint.data[0].mk,'B');
  assert.equal(by['가격'].markPoint.data[1].mk,'S');
  assert.equal(option.tooltip.trigger,'axis');
  assert.equal(option.dataZoom.length,2);
  assert.ok(option.legend.data.includes('시그널 점수'));
  if(mode==='simple'){
    assert.ok(!by['단기평균(20일)']);
    const tip=option.tooltip.formatter([
      {axisValue:'2026-10-01',seriesName:'가격',data:100},
      {axisValue:'2026-10-01',seriesName:'시그널 점수',data:1.2}]);
    assert.match(tip,/100원/);assert.match(tip,/\+1\.20/);assert.match(tip,/당시 저장한 판정/);
  }else assert.ok(by['단기평균(20일)']&&by['중기(60일)']&&by['장기(120일)']);
  if(mode==='detail') assert.ok(by.RSI&&by.MACD&&by['외국인']&&by['기관']);
}
""")
