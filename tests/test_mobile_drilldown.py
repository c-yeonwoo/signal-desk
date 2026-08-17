"""모바일 드릴다운 — 누른 행 바로 아래에 차트를 끼운다(2026-08-17).

세로로 쌓이면(≤900px) 차트 카드가 종목 목록 **전체 뒤**에 온다. 200종목을 지나쳐야 차트가
나오므로 모바일에서는 사실상 안 보인다. 그래서 누른 행 다음에 `<tr>` 을 만들어 카드를 **옮긴다**.

## 카드를 복제하지 않고 옮긴다

같은 문서 안의 이동이라 ECharts 캔버스·상태·핸들러가 그대로 살아 있다. 복제하면 두 개가
갈라지고, 이 리포가 이미 겪은 *"같은 것을 두 곳에서 조립하지 않는다"* 를 화면에서 반복한다.

## 브레이크포인트는 640이 아니라 900이다

세로로 쌓이는 지점이 `@media (max-width:900px)` 다. `_SIG_NARROW_PX`(640)를 쓰면
태블릿(640~900)에서 카드가 목록 뒤에 그대로 남는다 — 열은 접혔는데 드릴은 안 되는 상태.

## 만들면서 잡은 두 결함 (둘 다 브라우저로 렌더해서 잡았다)

1. **재렌더가 카드를 파괴했다.** 카드가 표 안에 있는 상태로 `innerHTML` 을 덮으면
   `.sig-chart-pane` 도 `#signal-chart` 도 **DOM에서 사라져** 차트가 영영 안 돌아왔다
   (새로고침해야 복구). `renderSignalList` **맨 앞**에서 먼저 빼내야 한다.
2. **`resize` 하나에 기댈 수 없다.** 뷰포트를 375→1280으로 바꿨을 때 `resize` 0회 ·
   `matchMedia change` 0회였고(미디어쿼리는 재평가돼 레이아웃만 2열), 카드가 표 안에 낀 채
   남아 2열 레이아웃이 깨졌다. 그래서 경로를 셋 둔다.
"""

from __future__ import annotations

import re
from pathlib import Path

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"


def _src() -> str:
    return _HTML.read_text(encoding="utf-8")


def _code() -> str:
    """`//` 줄 주석을 지운 본문 — 설명에 적힌 식별자가 오탐이 된다."""
    return re.sub(r"^\s*//.*$", "", _src(), flags=re.M)


def test_breakpoint_matches_the_css_that_stacks_the_layout():
    """**640이 아니라 900이다.** 다르면 태블릿에서 열은 접히고 드릴은 안 되는 상태가 남는다."""
    code = _code()
    px = int(re.search(r"_SIG_STACKED_PX\s*=\s*(\d+)", code).group(1))
    stacking = re.search(r"@media\s*\(max-width:\s*(\d+)px\s*\)\s*\{[^}]*?\.sig-workspace\s*\{"
                         r"[^}]*flex-direction:\s*column", _src(), re.S)
    assert stacking, "세로로 쌓는 미디어쿼리를 못 찾았다 — 상수를 대조할 대상이 없다"
    assert px == int(stacking.group(1)), (
        f"JS {px}px vs CSS {stacking.group(1)}px — 브레이크포인트가 갈라졌다")


def test_the_pane_is_moved_not_cloned():
    """복제하면 두 개가 갈라진다 — ECharts 상태도 하나만 살아난다."""
    code = _code()
    i = code.index("function _drillInsert(")
    blk = code[i:i + 1400]
    assert "appendChild(pane)" in blk, "카드를 옮기지 않는다"
    for banned in ("cloneNode", "outerHTML", "innerHTML = pane"):
        assert banned not in blk, f"카드를 복제한다({banned})"


def test_render_extracts_the_pane_before_overwriting_the_table():
    """**이게 그 파괴 버그다.** 순서가 반대면 카드가 DOM에서 통째로 사라진다."""
    code = _code()
    i = code.index("function renderSignalList(){")
    head = code[i:i + 700]
    assert "_drillRestore()" in head, "표를 덮기 전에 카드를 빼내지 않는다"
    # `innerHTML` 대입보다 **앞**이어야 한다
    restore_at = head.index("_drillRestore()")
    write = re.search(r"el\.innerHTML\s*=", code[i:])
    if write:
        assert restore_at < write.start(), "카드를 빼내기 전에 표를 덮는다 — 카드가 사라진다"


def test_three_independent_sync_paths_exist():
    """이벤트 하나에 기대면 그 이벤트가 안 오는 환경에서 카드가 표 안에 낀 채 남는다.

    실측: 뷰포트 변경에서 `resize` 0회 · `matchMedia change` 0회였다.
    """
    code = _code()
    assert "_sigStackMQ.addEventListener('change', _drillSync)" in code, "경로① matchMedia 없음"
    assert code.count("_drillSync()") >= 2, "경로②③(resize 디바운스·재렌더) 중 하나가 없다"
    # 셋이 **같은 함수**를 불러야 한다 — 로직이 갈라지면 한쪽만 고쳐진다
    assert code.count("function _drillSync(") == 1


def test_resize_uses_the_single_existing_listener():
    """리스너를 새로 만들지 않는다 — 같은 일을 두 곳에서 시키면 한쪽이 남는다."""
    assert _code().count("addEventListener('resize'") == 1


def test_chart_is_resized_after_the_move():
    """0폭에서 init 된 차트를 되살릴 경로가 없다 — 옮긴 뒤 폭이 잡히면 맞춰야 한다."""
    code = _code()
    for fn in ("_drillInsert", "_drillRestore"):
        i = code.index(f"function {fn}(")
        assert "_resizeWhenLaidOut" in code[i:i + 1400], f"{fn} 이 리사이즈를 안 부른다"


def test_same_row_collapses_only_when_stacked():
    """데스크톱에서 접기까지 하면 2열 레이아웃에서 차트가 사라진다."""
    code = _code()
    i = code.index("function selectSignalRow(")
    blk = code[i:i + 800]
    assert "_sigStacked()" in blk and "_drillRestore()" in blk
    assert "_selectedTicker === ticker" in blk, "같은 행 재클릭 접기 경로가 없다"


def test_drill_row_styles_are_scoped_to_the_list():
    """`.drill-row` 스타일이 전역이면 다른 표에 새어 나간다."""
    src = _src()
    for rule in (".sig-list table.dtable tr.drill-row > td",
                 ".sig-list table.dtable tr.drill-row .sig-chart-pane"):
        assert rule in src, f"드릴 행 스타일이 목록 안으로 한정되지 않았다: {rule}"


def test_mobile_padding_is_reduced_only_inside_the_drill():
    """카드 기본 패딩(24px)은 데스크톱 값이라 375px에서 48px(14%)을 먹는다 — 실측 295→319px.

    카드 자체를 건드리면 데스크톱까지 좁아지므로 드릴 안에서만 줄인다.
    """
    src = _src()
    i = src.index(".sig-list table.dtable tr.drill-row .sig-chart-pane")
    assert re.search(r"padding:\s*12px", src[i:i + 200]), "드릴 안 패딩을 줄이지 않았다"
