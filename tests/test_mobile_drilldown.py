"""종목 목록·상세 전환: 차트 DOM을 표 안으로 옮기지 않고 선택·포커스를 보존한다."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_HTML = _ROOT / "src/signal_desk/web/index.html"


def _node(script):
    if not shutil.which("node"):
        pytest.skip("Node is needed for the stock workspace")
    result = subprocess.run(["node", "-e", script], cwd=_ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


_WORKSPACE = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
function node(){
  const classes=new Set();
  return {style:{},textContent:'',hidden:false,attrs:{},
    classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),
      toggle(c,yes){if(yes) classes.add(c);else classes.delete(c);}},
    setAttribute(k,v){this.attrs[k]=v;},
    focus(){doc.activeElement=this;},scrollIntoView(){this.scrolled=true;},
    replaceChildren(){this.cleared=true;}};
}
const doc={activeElement:null},ids={},workspace=node(),pane=node();
for(const id of ['sig-search','signal-chart-title','chart-asof','signal-about',
  'signal-selection-empty','signal-hero','signal-company-review','signal-chart']) ids[id]=node();
pane.contains=el=>el===ids['signal-chart-title'];
const rows=['AAA','BBB'].map(ticker=>{
  const row=node();row.dataset={ticker};row.button=node();
  row.querySelector=()=>row.button;return row;
});
doc.getElementById=id=>ids[id];
doc.querySelector=selector=> selector==='.sig-workspace'?workspace:selector==='.sig-chart-pane'?pane
  :rows.find(row=>selector.includes(`"${row.dataset.ticker}"`))?.button;
doc.querySelectorAll=()=>rows;
const mq={matches:true,addEventListener(type,fn){this.listener=fn;}};
const calls={shown:[],resized:0,disposed:0,abort:0,scroll:[]};
const chartIdentity={};
const ctx=vm.createContext({document:doc,window:{scrollY:375,matchMedia:()=>mq,
  scrollTo:options=>calls.scroll.push(options)},CSS:{escape:v=>v},
  _selectedTicker:null,_signalItems:{AAA:{kind:'BUY'},BBB:{kind:'HOLD'}},
  _signalChart:chartIdentity,_chartCache:{ticker:'AAA'},_signalDetailSeq:0,
  _chartAbort:{abort(){calls.abort++;}},
  _resizeWhenLaidOut:chart=>{assert.equal(chart,chartIdentity);calls.resized++;},
  _disposeSignalChart:()=>calls.disposed++,
  renderSelectedLensDetail:ticker=>calls.lens=ticker,
  showSignalChart:(ticker,item)=>calls.shown.push({ticker,item})});
const start=html.indexOf('const _SIG_STACKED_PX =');
vm.runInContext(html.slice(start,html.indexOf('// 배지 색을',start)),ctx);
"""


def test_mobile_selection_back_and_reopen_keep_single_chart_and_focus():
    _node(_WORKSPACE + r"""
ctx.selectSignalRow('AAA');
assert.equal(ctx._selectedTicker,'AAA');
assert.equal(workspace.classList.contains('detail-open'),true);
assert.equal(ids['signal-selection-empty'].hidden,true);
assert.equal(doc.activeElement,ids['signal-chart-title']);
assert.equal(pane.scrolled,true);
assert.equal(rows[0].button.attrs['aria-pressed'],'true');
assert.equal(rows[1].button.attrs['aria-pressed'],'false');
ctx.backToSignalList();
assert.equal(workspace.classList.contains('detail-open'),false);
assert.equal(doc.activeElement,rows[0].button);
assert.equal(calls.scroll.at(-1).top,375);
assert.equal(ctx._selectedTicker,'AAA');
assert.equal(calls.disposed,0);
// 같은 종목을 다시 눌러도 접기 동작으로 오해하지 않고 상세를 연다.
ctx.selectSignalRow('AAA');
assert.equal(workspace.classList.contains('detail-open'),true);
ctx.selectSignalRow('BBB');
assert.equal(rows[0].button.attrs['aria-pressed'],'false');
assert.equal(rows[1].button.attrs['aria-pressed'],'true');
assert.equal(calls.shown.at(-1).ticker,'BBB');
mq.matches=false;mq.listener();
assert.ok(calls.resized>0);
assert.equal(ctx._signalChart,chartIdentity);
""")


def test_desktop_selection_does_not_steal_focus_or_scroll():
    _node(_WORKSPACE + r"""
mq.matches=false;rows[0].button.focus();
ctx.selectSignalRow('AAA');
assert.equal(doc.activeElement,rows[0].button);
assert.equal(pane.scrolled,undefined);
assert.equal(calls.scroll.length,0);
ctx.syncSignalLayout();
assert.equal(ctx._signalChart,chartIdentity);
assert.equal(calls.disposed,0);
""")


def test_hidden_selection_clears_price_chart_and_invalidates_inflight_request():
    _node(_WORKSPACE + r"""
ctx.selectSignalRow('AAA');
ctx.clearSignalSelection('검색 결과에서 종목을 선택하세요.');
assert.equal(ctx._selectedTicker,null);
assert.equal(ctx._signalDetailSeq,1);
assert.equal(ctx._chartCache,null);
assert.equal(calls.abort,1);
assert.equal(calls.disposed,1);
assert.equal(workspace.classList.contains('detail-open'),false);
assert.equal(ids['signal-chart'].cleared,true);
assert.equal(ids['signal-hero'].style.display,'none');
assert.equal(ids['signal-company-review'].style.display,'none');
assert.equal(ids['signal-selection-empty'].hidden,false);
assert.equal(doc.activeElement,ids['sig-search']);
assert.ok(rows.every(row=>row.button.attrs['aria-pressed']==='false'));
// 선택 행이 필터에서 사라진 경우 뒤로 가기는 검색으로 돌아간다.
ctx._selectedTicker='REMOVED';ctx.backToSignalList();
assert.equal(doc.activeElement,ids['sig-search']);
""")


def test_mobile_visibility_breakpoint_matches_javascript():
    src = _HTML.read_text(encoding="utf-8")
    px = int(re.search(r"_SIG_STACKED_PX\s*=\s*(\d+)", src).group(1))
    # CSS의 실제 모바일 전환 규칙을 대조한다. 차트를 목록 행 안에 삽입하는 계약은 폐기했다.
    mobile = re.search(r"@media\s*\(max-width:(\d+)px\)\s*\{\s*#view-signal \.sig-workspace", src)
    assert mobile and int(mobile.group(1)) == px
    mobile = src[mobile.start():]
    assert ".sig-workspace.detail-open .sig-list { display:none; }" in mobile
    assert ".sig-workspace.detail-open .sig-chart-pane { display:flex; }" in mobile
