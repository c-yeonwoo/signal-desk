"""The first screen must keep frozen guidance and three performance scopes separate."""

from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from signal_desk import api


def test_latest_home_diagnosis_is_user_scoped_and_never_recomputed(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "_uid", lambda request: 17)
    monkeypatch.setattr(api.db, "portfolio_snapshot_latest", lambda uid, market: (
        seen.append((uid, market)) or {"as_of": "2026-09-23", "created": 1,
             "source": "daily_close", "data_quality": "complete",
             "payload": {"summary": {"positions": 2},
                         "holdings": [{"price_as_of": "2026-09-22"}, {"price_as_of": "2026-09-23"}],
                         "guidance": [{"action": "단일 종목 비중 검토", "current_pct": 41,
                                       "limit_pct": 30}], "trade_plan": {"instructions": [{"qty": 99}]}}}))
    request = Request({"type": "http", "method": "GET", "path": "/api/portfolio/latest", "headers": []})
    out = api.portfolio_latest_get(request, "us")
    assert seen == [(17, "us")]
    assert out["guidance"][0]["action"] == "단일 종목 비중 검토"
    assert "trade_plan" not in out  # A frozen diagnosis is not an order ticket.
    assert out["as_of"] == "2026-09-23"
    assert out["price_asof_range"] == {"first": "2026-09-22", "last": "2026-09-23"}


def test_dashboard_assets_are_served_from_explicit_routes():
    client = TestClient(api.app)
    assert client.get("/home.js").status_code == 200
    assert client.get("/home.css").status_code == 200
    assert client.get("/other.js").status_code == 404


def test_home_ui_preserves_provenance_and_ignores_old_market_response():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the home component")
    root = Path(__file__).resolve().parents[1]
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const code = fs.readFileSync('src/signal_desk/web/home.js','utf8');
const html = fs.readFileSync('src/signal_desk/web/index.html','utf8');
assert.match(html,/계좌 전체 수익률 아님/);
assert.doesNotMatch(html,/손절 -7% \/ 익절 \+15%/);
const els = {};
for (const m of html.matchAll(/id="(home-[^"]+|investment-home)"/g))
  els[m[1]] = {textContent:'', previousElementSibling:{querySelector:()=>({textContent:''})}};
const document = {getElementById:id=>els[id]};
const responses = {
  '/api/portfolio/latest?market=kr':{ready:true,as_of:'2026-09-23',data_quality:'complete',guidance:[{action:'국내 검토',reason:'국내 위험'}]},
  '/api/portfolio/latest?market=us':{ready:false},
  '/api/live/copy-policy':{source_style:'balanced',follow_pct:20,configured:true},
  '/api/my-broker-account':{ready:false},
  '/api/my-performance?market=kr':{points:[{daily_return_pct:2,observed_at:1}]},
  '/api/my-performance?market=us':{points:[]},
  '/api/reference-performance?market=kr':{bots:[{style:'balanced',return_pct:5,curve:[]}]},
  '/api/reference-performance?market=us':{bots:[{style:'balanced',return_pct:-3,curve:[]}]},
};
let releaseKr;
const fetch = url => url.endsWith('market=kr') && url.includes('portfolio/latest')
  ? new Promise(resolve=>{releaseKr=()=>resolve({ok:true,json:async()=>responses[url]})})
  : Promise.resolve({ok:true,json:async()=>responses[url]});
const context = vm.createContext({window:{},document,fetch,Date,Number});
vm.runInContext(code,context);
(async()=>{
  const first = context.window.InvestmentHome.load({market:'kospi',profile:{}});
  const second = context.window.InvestmentHome.load({market:'us',profile:{}});
  releaseKr();
  await Promise.all([first,second]);
  assert.equal(els['home-market'].textContent,'해외 · USD');
  assert.equal(els['home-action-title'].textContent,'내 보유 입력·진단부터');
  assert.equal(els['home-paper-return'].textContent,'-3.00%');
  assert.equal(els['home-real-return'].textContent,'관측 없음');
  context.window.InvestmentHome.updateChange({ready:true,changes_total:3});
  assert.match(els['home-change'].textContent,/해외.*섞지 않습니다/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True, cwd=root, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
