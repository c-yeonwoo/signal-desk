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


def test_signal_sort_is_default_and_initial_selection_matches_visible_order():
    html = HTML.read_text(encoding="utf-8")
    assert '<option value="signal">시그널순 (매수 우선)</option>' in html
    assert '<option value="score">점수순 (참고)</option>' in html
    assert "else rows = sortSignalRows(rows, sort);" in html
    assert "const firstVisibleTicker = renderSignalList();" in html
    assert "else if (firstVisibleTicker) selectSignalRow(firstVisibleTicker);" in html
