"""Exercise draft/server races in the shipped UI without a browser or live account."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_copy_settings_keep_drafts_and_previews_consistent():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the inline UI state machine")
    root = Path(__file__).resolve().parents[1]
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync('src/signal_desk/web/index.html', 'utf8');
const code = 'let _liveCopyLoaded' + html.split('let _liveCopyLoaded')[1]
  .split('async function loadPortfolioProfile')[0];
const elements = {};
for (const match of html.matchAll(/id="(live-copy-[^"]+)"/g)) {
  elements[match[1]] = {value:'', textContent:'', innerHTML:'', style:{}, disabled:false, validity:{valid:true}};
}
const document = {getElementById:id => elements[id], querySelectorAll:() => []};
const saved = {source_style:'balanced', follow_pct:50, max_order_pct:10,
  max_daily_buy_pct:20, max_position_pct:15, min_cash_pct:25, configured:true, updated:1};
const response = d => ({ok:true, json:async()=>d});
const context = vm.createContext({document, fmtNum:n=>String(n), esc:s=>String(s),
  fetch:async url=>response(url.includes('copy-events') ? {events:[]} : saved)});
const run = source => vm.runInContext(source, context);
run(code);
(async()=>{
  await run('loadLiveCopyPolicy()');
  assert.equal(elements['live-copy-follow'].value, 50);
  assert.equal(elements['live-copy-save'].disabled, true);
  elements['live-copy-follow'].value = '30';
  run('liveCopyDraftChanged()');
  assert.equal(elements['live-copy-example'].textContent, '30만원');
  assert.equal(elements['live-copy-preview'].disabled, true);
  assert.equal(elements['live-copy-save'].disabled, false);

  // Changing a field while the PUT is in flight must not lose the newer edit.
  let finishSave;
  context.fetch = async(url, opts) => opts?.method === 'PUT'
    ? new Promise(resolve => { finishSave=()=>resolve(response({...saved,follow_pct:30})); })
    : response({events:[]});
  const saving = run("saveLiveCopyPolicy(document.getElementById('live-copy-save'))");
  elements['live-copy-follow'].value = '40';
  run('liveCopyDraftChanged()');
  finishSave();
  await saving;
  assert.equal(elements['live-copy-follow'].value, '40');
  assert.equal(elements['live-copy-preview'].disabled, true);
  assert.match(elements['live-copy-policy-note'].textContent, /저장하지 않은/);

  // Loading a saved draft restores the matching state, but a pending preview
  // must never paint an answer for an older price over the current input.
  context.fetch = async url=>response(url.includes('copy-events') ? {events:[]} : saved);
  await run('loadLiveCopyPolicy()');
  elements['live-copy-event'].value = '1';
  elements['live-copy-price'].value = '100';
  let finishPreview;
  context.fetch = async()=>new Promise(resolve=>{
    finishPreview=()=>resolve(response({reason:'OLD PRICE RESULT'}));
  });
  const previewing = run("previewLiveCopy(document.getElementById('live-copy-preview'))");
  elements['live-copy-price'].value = '200';
  run('clearLiveCopyPreview()');
  finishPreview();
  await previewing;
  assert.doesNotMatch(elements['live-copy-result'].textContent, /OLD PRICE RESULT/);

  // A later load failing authorization must disable old cached actions.
  context.fetch = async()=>({ok:false,json:async()=>({detail:'소유자만 접근'})});
  await run('loadLiveCopyPolicy()');
  assert.equal(elements['live-copy-save'].disabled, true);
  assert.equal(elements['live-copy-preview'].disabled, true);
  assert.match(elements['live-copy-policy-note'].textContent, /소유자만 접근/);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True, cwd=root, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
