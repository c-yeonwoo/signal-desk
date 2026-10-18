"""시그널 목록은 최종 매수 판정이 원점수보다 먼저 오도록 정렬한다."""

import json
import subprocess
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html"


def test_signal_sort_prioritizes_final_action_and_keeps_score_mode():
    html = HTML.read_text(encoding="utf-8")
    start = html.index("const _SIGNAL_KIND_ORDER =")
    end = html.index("function renderSignalList(){", start)
    script = html[start:end] + """
const rows = [
  {ticker:'HOLD_HIGH', kind:'HOLD', score:3},
  {ticker:'BUY_LOW', kind:'BUY', score:1.6},
  {ticker:'STRONG_LOW', kind:'STRONG_BUY', score:1.5},
  {ticker:'BUY_HIGH', kind:'BUY', score:2},
  {ticker:'SELL', kind:'SELL', score:-1},
  {ticker:'STRONG_SELL', kind:'STRONG_SELL', score:-2},
];
const original = rows.map(r => r.ticker);
process.stdout.write(JSON.stringify({
  signal: sortSignalRows(rows, 'signal').map(r => r.ticker),
  score: sortSignalRows(rows, 'score').map(r => r.ticker),
  original: rows.map(r => r.ticker),
  unchanged: sortSignalRows(rows, 'mktcap') === rows,
}));
"""
    result = subprocess.run(
        ["node", "--input-type=commonjs"], input=script, text=True,
        capture_output=True, check=True,
    )
    output = json.loads(result.stdout)
    assert output["signal"] == [
        "STRONG_LOW", "BUY_HIGH", "BUY_LOW", "HOLD_HIGH", "SELL", "STRONG_SELL",
    ]
    assert output["score"][:4] == ["HOLD_HIGH", "BUY_HIGH", "BUY_LOW", "STRONG_LOW"]
    assert output["original"] == [
        "HOLD_HIGH", "BUY_LOW", "STRONG_LOW", "BUY_HIGH", "SELL", "STRONG_SELL",
    ]
    assert output["unchanged"]


def test_default_sort_and_initial_selection_respect_mobile_list_and_deep_links():
    html = HTML.read_text(encoding="utf-8")
    assert '<option value="signal">시그널순 (매수 우선)</option>' in html
    assert '<option value="score">점수순 (참고)</option>' in html
    start = html.index("function applySignalData(")
    code = html[start:html.index("async function loadSignals(", start)]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
let narrow=false;
const selected=[],refreshedDetails=[],painted=[],paintedItems=[],cleared=[];
const context=vm.createContext({Object,document:{getElementById:()=>({})},
  _backtestByKind:{},_sigTodayMeta:{},_signalItems:{},_signalRows:null,_lensSnapshot:null,
  _selectedTicker:null,_pendingSignalTicker:null,_appliedSignalSnapshot:null,_sigStacked:()=>narrow,
  populateScreenSectors:()=>{},renderScreenPresets:()=>{},renderSignalLensPanel:()=>{},
  renderSignalList:()=> 'BUY_VISIBLE_FIRST',
  selectSignalRow:ticker=>selected.push(ticker),
  showSignalChart:(ticker,item)=>refreshedDetails.push({ticker,item}),
  _paintSignalDetail:(ticker,item)=>{painted.push(ticker);paintedItems.push(item);},
  clearSignalSelection:reason=>cleared.push(reason)});
""" + "\nvm.runInContext(" + json.dumps(code) + ",context);\n" + r"""
const data={ready:true,items:[{ticker:'HOLD_SERVER_FIRST'},{ticker:'BUY_VISIBLE_FIRST'}]};
context.applySignalData(data,null);
assert.deepEqual(selected,['BUY_VISIBLE_FIRST']);
narrow=true;selected.length=0;
context.applySignalData(data,null);
assert.deepEqual(selected,[]); // 모바일은 목록부터, 첫 종목 자동 상세 진입 금지.
context._pendingSignalTicker='HOLD_SERVER_FIRST';
context.applySignalData(data,null);
assert.deepEqual(selected,['HOLD_SERVER_FIRST']); // 사용자가 연 딥링크는 모바일도 존중.
assert.equal(context._pendingSignalTicker,null);
selected.length=0;context._selectedTicker='BUY_VISIBLE_FIRST';
const fullDetail={...context._signalItems.BUY_VISIBLE_FIRST,
  kb:{summary:'원문에서 확인한 근거'},about:'기업 설명',target:{resistance:220}};
context._signalItems.BUY_VISIBLE_FIRST=fullDetail;
context.applySignalData(data,null);
assert.deepEqual(selected,[]);assert.deepEqual(painted,['BUY_VISIBLE_FIRST']);
assert.equal(context._signalItems.BUY_VISIBLE_FIRST,fullDetail);
assert.equal(paintedItems.at(-1),fullDetail); // 동일 캐시의 요약행이 보강된 상세를 지우지 않는다.
assert.equal(data.items[1].kb,undefined); // 목록 원본에 상세를 섞어 넣지도 않는다.
const refreshed={...data,items:data.items.map(item=>({...item,price:210}))};
context.applySignalData(refreshed,null);
assert.deepEqual(selected,[]); // 모바일 목록 복귀 상태와 포커스를 바꾸지 않는다.
assert.equal(refreshedDetails.length,1); // 새 응답은 선택 화면 전환 없이 상세·차트만 재조회한다.
assert.equal(refreshedDetails[0].ticker,'BUY_VISIBLE_FIRST');
assert.equal(refreshedDetails[0].item,refreshed.items[1]);
assert.equal(context._signalItems.BUY_VISIBLE_FIRST,refreshed.items[1]);
assert.equal(context._appliedSignalSnapshot,refreshed);
assert.equal(paintedItems.length,1); // 새 가격과 예전 상세를 섞어 페인트하지 않는다.
context.applySignalData({ready:true,items:[]},null);
assert.equal(cleared.length,1);assert.deepEqual(Object.keys(context._signalItems),[]);
"""
    result = subprocess.run(["node", "--input-type=commonjs"], input=script, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
