"""Archive continuity is visible without implying that IDs certify raw bytes."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_evidence_ops_displays_missing_corrupt_and_anchor_separately():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser component tests")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('src/signal_desk/web/index.html', 'utf8');
const source = 'function evidenceOpsHtml(d){' + html.split('function evidenceOpsHtml(d){', 2)[1].split('async function loadDataHealth(', 1)[0];
const ctx = vm.createContext({esc:s => String(s).replaceAll('<','&lt;').replaceAll('>','&gt;')});
vm.runInContext(source, ctx);
const render = item => vm.runInContext('evidenceOpsHtml(' + JSON.stringify(item) + ')', ctx);
const base = {evidence_activity:{window_days:30,sources:{}},
  archive_continuity:{app_db_boot_count:null, archives:{
    dart:{status:'not_recorded',observations:0,first_id:null},
    sec:{status:'archive_error',observations:null,first_id:null},
    fed_g17:{status:'recorded',observations:2,first_id:'g17-anchor',first_available_at:'2026-10-06T07:00:00Z'}}}};
const page = render(base);
assert.match(page, /보존 연속성 확인 · DART 0건 · 연준 2건 · SEC 오류/);
assert.match(page, /앱 DB 부팅 확인 불가/);
assert.match(page, /보존 파일 읽기 실패/);
assert.match(page, /g17-anchor/);
assert.match(page, /일치만으로 원문 무결성을 증명하지 않습니다/);
assert.doesNotMatch(page, /앱 DB 부팅 0회/);
const escaped = render({...base, archive_continuity:{...base.archive_continuity,
  archives:{...base.archive_continuity.archives,
    fed_g17:{status:'recorded',observations:1,first_id:'<script>'}}}});
assert.match(escaped, /&lt;script&gt;/);
assert.doesNotMatch(escaped, /<script>/);
"""
    result = subprocess.run([node], input=script, text=True, capture_output=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
