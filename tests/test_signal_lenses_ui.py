"""관점 비교는 조회용이며 알 수 없음/안전 차단을 필터에서 통과시키지 않는다."""

import json
import subprocess
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html"


def test_lens_controls_and_script_syntax():
    html = HTML.read_text()
    assert 'id="sig-lenses"' in html
    assert 'id="sig-lens-event"' in html and 'id="sig-lens-entry"' in html
    assert "기본 판단·봇·실주문에는 적용되지 않습니다" in html
    assert 'id="sig-lens-macro_release"' in html
    assert "경제 발표는 사전 예상치·실제값이 있을 때만" in html
    # 앱은 한 파일에 외부·내부 스크립트가 섞여 있다. 인라인 스크립트만 파싱한다.
    scripts = []
    for part in html.split("<script"):
        if ">" not in part or "</script>" not in part:
            continue
        opening, body = part.split(">", 1)
        if "src=" not in opening:
            scripts.append(body.split("</script>", 1)[0])
    assert scripts
    for script in scripts:
        result = subprocess.run(["node", "--check", "--input-type=commonjs"],
                                input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_lens_filter_does_not_promote_missing_or_blocked_rows():
    html = HTML.read_text()
    start = html.index("function lensPass(row, modes){")
    end = html.index("function setSignalLensMode(", start)
    function_source = html[start:end]
    cases = [
        ({"lens_results": {}}, {"event": "off", "entry": "include"}, True),
        ({"lens_results": {}}, {"event": "require", "entry": "off"}, False),
        ({"lens_results": {"event": {"verdict": "unavailable"}}},
         {"event": "require", "entry": "off"}, False),
        ({"lens_results": {"event": {"verdict": "pass"}}},
         {"event": "require", "entry": "off"}, True),
        ({"decision_buy_blocked": True,
          "lens_results": {"event": {"verdict": "pass"}}},
         {"event": "require", "entry": "off"}, False),
        ({"gate_blocked": True,
          "lens_results": {"entry": {"verdict": "pass"}}},
         {"event": "off", "entry": "require"}, False),
        ({"lens_results": {"event": {"verdict": "pass"},
                           "entry": {"verdict": "hold"}}},
         {"event": "require", "entry": "require"}, False),
    ]
    js = function_source + "\nconst cases = " + json.dumps(cases) + ";\n" \
        + "process.stdout.write(JSON.stringify(cases.map(([row,modes]) => lensPass(row,modes))));"
    result = subprocess.run(["node", "--input-type=commonjs"], input=js,
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [expected for _, _, expected in cases]


def test_lens_controls_do_not_call_trading_endpoints():
    html = HTML.read_text()
    block = html[html.index("function lensPass(row, modes){"):
                 html.index("// #14 기회 유형 필터")]
    assert "fetch(" not in block
    assert "/api/live" not in block and "/api/bot" not in block
