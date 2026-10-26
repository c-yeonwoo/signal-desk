from pathlib import Path
import shutil
import subprocess

import pytest


def test_research_ui_preserves_missing_values_and_refresh_errors():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node needed for actual research renderer")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let report=JSON.parse(fs.readFileSync('src/signal_desk/research_reports/entry_timing.json'));
report.status='available';
const holder={innerHTML:'',setAttribute(){},removeAttribute(){},querySelectorAll(){return []}};
const button={disabled:false}; let options, ok=true, interval;
const document={hidden:false,querySelector(){return null},getElementById(id){return id==='entry-timing-body'?holder:id==='entry-timing-refresh'?button:{getClientRects:()=>[1]}}};
const window={addEventListener(){},echarts:{init:()=>({dispose(){},resize(){},setOption(o){options=o}})}};
let calls=[];
const context=vm.createContext({window,document,Date,Number,AbortSignal,
getComputedStyle:()=>({getPropertyValue:()=> '#243CDB'}),
location:{hash:'#admin/verify'}, setInterval:fn=>{interval=fn},
fetch:async(url,opts)=>{calls.push(url);assert.equal(opts.cache,'no-store');return{ok,json:async()=>report}}});
vm.runInContext(fs.readFileSync('src/signal_desk/web/entry-timing.js','utf8'),context);
(async()=>{
await window.loadEntryTiming();
assert.match(holder.innerHTML,/-0.45%/); assert.match(holder.innerHTML,/-1.14%/);
assert.match(holder.innerHTML,/채택하지 않음/); assert.match(holder.innerHTML,/판단/);
assert.equal(options.series[0].data.length,7);
assert(Math.abs(options.series[0].data.at(-1)-report.paths['immediate:base'].conditional_account_return_pct)<1e-10);
assert.equal(button.disabled,false);
report.paths['immediate:base'].conditional_account_return_pct=null;
report.paths['immediate:base'].rows[1].net_pnl=null;
report.paths['immediate:base'].rows[1].trade_net_return_pct=null;
report.paths['immediate:base'].rows[1].status='exit_missing_position_unresolved';
report.paths['immediate:base'].rows[0].issuer='<img src=x onerror=alert(1)>';
await window.loadEntryTiming();
assert.match(holder.innerHTML,/계산 보류/);assert.match(holder.innerHTML,/보유 미해결/);
assert.doesNotMatch(holder.innerHTML,/<img/);
assert.equal(options.series[0].data[2],null);assert.equal(options.series[0].data.at(-1),null);
ok=false;await window.loadEntryTiming(); assert.match(holder.innerHTML,/로그인 상태/);assert.equal(button.disabled,false);
ok=true;report={status:'not_available'};await window.loadEntryTiming();assert.match(holder.innerHTML,/아직 배포된/);
const n=calls.length;document.hidden=true;interval();assert.equal(calls.length,n);
assert(calls.every(url=>url==='/api/admin/research/entry-timing'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
