"""첫 화면 요청과 읽기 쉬운 설명의 회귀 계약."""

from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from signal_desk import api


WEB = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"


def test_index_revalidates_without_retransferring_html():
    client = TestClient(api.app)
    first = client.get("/")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["etag"]
    second = client.get("/", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert second.content == b""


def test_signal_screen_does_not_load_hidden_portfolio_home():
    html = WEB.read_text(encoding="utf-8")
    tab = html.split("function switchTab(t){", 1)[1].split("const _SEGS =", 1)[0]
    market = html.split("function switchSignalMarket(m){", 1)[1].split("// 시장별 응답 캐시", 1)[0]
    assert "InvestmentHome.load" not in tab + market
    assert "if (seg === 'rebal')" in html and "InvestmentHome.load" in html


def test_market_detail_does_not_show_or_fetch_relative_strength_leaderboard():
    html = WEB.read_text(encoding="utf-8")
    details = html.split('<div id="macro-detail"', 1)[1].split("</main>", 1)[0]
    loader = html.split("function loadMacroDetails(){", 1)[1].split("}\n", 1)[0]
    assert 'id="relstr-card"' not in details
    assert "loadRelativeStrength" not in loader
    assert "'/api/relative-strength'" not in html
    assert '/api/relative-strength' not in Path(api.__file__).read_text(encoding="utf-8")


def test_signal_list_renders_without_waiting_for_backtest():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the signal loader")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync('src/signal_desk/web/index.html', 'utf8');
const start = html.indexOf('async function loadSignals(opts){');
const end = html.indexOf('// 시총(', start);
const calls = [], painted = [], body = {className:'',textContent:''};
const ctx = vm.createContext({
  document:{getElementById:()=>body}, Date, Promise, Set,
  fetch:url=>{calls.push(url);return Promise.resolve({json:async()=>
    url.includes('favorites') ? {favorites:[]} : {ready:true,items:[{ticker:'005930'}]}});},
  applySignalData:(d,bt)=>painted.push([d.items.length,bt]),
  _sigMarket:'kospi', _sigCache:{}, _SIG_TTL:180000, _sigLoadSeq:0,
  _backtestCache:null, _favorites:new Set(),
});
vm.runInContext(html.slice(start,end),ctx);
(async()=>{
  await ctx.loadSignals();
  assert.deepEqual(calls,['/api/signals?market=kospi','/api/favorites']);
  assert.deepEqual(painted,[[1,null]]);
  ctx._sigMarket='us';
  ctx._backtestCache={ready:true,by_signal:[{kind:'BUY'}]};
  ctx._sigCache.us={d:{ready:true,items:[{ticker:'AAPL'}]},ts:Date.now()};
  await ctx.loadSignals();
  assert.deepEqual(calls,['/api/signals?market=kospi','/api/favorites']);
  assert.deepEqual(painted,[[1,null],[1,null]]);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    out = subprocess.run([node, "-e", script], cwd=WEB.parents[3],
                         text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr


def test_readable_text_keeps_full_source_and_escapes_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the copy helper")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync('src/signal_desk/web/index.html', 'utf8');
const code = html.slice(html.indexOf('function readableText('),html.indexOf('function fmtNum('));
const ctx = vm.createContext({esc:s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))});
vm.runInContext(code,ctx);
const rendered = ctx.readableText('쉽게 말하면, 가격 흐름은 약합니다. <script>위험</script>은 더 확인해야 합니다.',
                                  '전체 해설',['<img src=x>']);
assert.match(rendered, /가격 흐름은 약합니다/);
assert.match(rendered, /전체 해설/);
assert.match(rendered, /&lt;script&gt;위험&lt;\/script&gt;/);
assert.match(rendered, /&lt;img src=x&gt;/);
assert.doesNotMatch(rendered, /<script>|<img/);
"""
    out = subprocess.run([node, "-e", script], cwd=WEB.parents[3],
                         text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr
