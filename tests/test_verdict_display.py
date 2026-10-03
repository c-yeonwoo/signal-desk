"""The first-screen verdict must not turn a control percentile into return outperformance."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_verdict_copy_and_color_follow_locked_decision_not_percentile_alone():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser component tests")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('src/signal_desk/web/index.html', 'utf8');
const source = 'const _T = {' + html.split('const _T = {', 2)[1].split('function toggleTrustLegend()', 1)[0];
const ctx = vm.createContext({esc:String, fmtNum:(n,d)=>n == null ? '-' : Number(n).toFixed(d)});
vm.runInContext(source, ctx);
const render = item => vm.runInContext('verdictRow(' + JSON.stringify(item) + ')', ctx);
const base = {ready:true, threshold_pct:99.15, n_looks_total:6, n_registered:5};
const pass = render({...base, status:'locked', verdict:'판별력 있음', percentile:99.2,
  verdict_why:'최악 위상도 우위'});
assert.match(pass, /무작위 대조 성적의 99\.2백분위/);
assert.match(pass, /등록 문턱 99\.15백분위/);
assert.match(pass, /var\(--sig-buy\)/);
assert.doesNotMatch(pass, /무작위보다.*% 위/);
const phaseFail = render({...base, status:'locked', verdict:'판정 불가', percentile:99.2,
  verdict_why:'위상에 따라 달라짐'});
assert.match(phaseFail, /위상에 따라 달라짐/);
assert.doesNotMatch(phaseFail, /var\(--sig-buy\)/);
const missing = render({...base, status:'locked', verdict:'판정 불가', percentile:null,
  verdict_why:'대조군 없음'});
assert.match(missing, /대조 백분위 미산출/);
assert.doesNotMatch(missing, /-% 위/);
const pending = render({...base, status:'pending', verdict:'판정 보류', percentile:99.9,
  requirement:{min_effective_periods:30,effective_periods:12,effective_periods_source:'estimated',
               min_pit_dates:150,pit_dates:62}});
assert.match(pending, /평가기간 30개 중 <b>12<\/b>개\(추정\)/);
assert.doesNotMatch(pending, /99\.9/);
const invalid = render({...base, status:'invalidated', verdict:'무효', percentile:99.9,
  verdict_why:'설정이 바뀜'});
assert.match(invalid, /이전 판정은 무효/);
assert.match(invalid, /설정이 바뀜/);
assert.doesNotMatch(invalid, /99\.9/);
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
