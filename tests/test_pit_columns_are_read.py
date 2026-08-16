"""PIT 스냅샷에 쓰는 컬럼은 **읽는 곳이 있어야 한다**(2026-08-16).

`pre_run_up_pct` 가 이 검사가 없어서 빠져나갔다 — `snapshot_signals` 가 매일 값을 쓰는데
`pre_move.summary` 는 호출자 0이었고, 라우트·화면 어디에도 없었다. 데이터는 한 달 쌓였고
아무도 보지 않았다.

이 리포는 같은 병에 대해 이미 두 개의 검사를 갖고 있다 —
`test_every_api_route_has_a_caller_or_a_stated_reason`(라우트),
`test_every_push_helper_has_a_caller`(푸시). **PIT 컬럼만 빠져 있었고**, 하필 거기가
"측정하려고 만든 것"이 모이는 자리다. 쌓기만 하고 안 보는 관측은 영원히 안 본다.

여기서 '읽는다'는 **PIT 이력을 소비하는 쪽**에서 참조한다는 뜻이다 — 오늘 화면에 그리는 것은
읽기가 아니다(그건 `SignalResult` 필드지 스냅샷이 아니다). 그래서 소비자 목록을 고정한다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "signal_desk"

# PIT 이력(`store.load_signal_history` 행)을 실제로 소비하는 곳. 여기서 참조돼야 '읽었다'이다.
_CONSUMERS = (
    _SRC / "signals" / "accuracy.py",
    _SRC / "signals" / "pre_move.py",
    _SRC / "signals" / "harness.py",
    _SRC / "signals" / "pick_reason.py",
    _SRC / "signals" / "kb_coverage.py",
    _SRC / "store.py",            # scores_from_pit·PIT 슬라이스 등
    _SRC / "api.py",              # 라우트가 행을 그대로 내보내는 경우
)

# 읽는 곳이 없어도 되는 컬럼 — **이유를 하나씩 적는다.** 통째로 스킵하면 새 고아가 조용히 섞인다.
_ALLOWED = {
    "date": "조인 키 — 값이 아니라 축이다",
    "ticker": "조인 키 — 값이 아니라 축이다",
}
_MAX_ALLOWED = 4          # 목록이 자라기만 하는 것을 막는다


def _snapshot_columns() -> set[str]:
    """`store.snapshot_signals` 가 rows에 넣는 키를 **AST로** 뽑는다.

    문자열 검색이 아니라 AST인 이유: 주석·독스트링에 적힌 컬럼명이 오탐이 된다
    (이 리포에서 세 번 반복된 실수다).
    """
    tree = ast.parse((_SRC / "store.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "snapshot_signals")
    cols: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    cols.add(k.value)
    return cols


def _strip_write_site(src: str) -> str:
    """`snapshot_signals` 본문을 지운다 — **쓰기 지점이 자기 자신을 읽기로 세면** 검사가
    모든 컬럼을 통과시킨다(양성 대조군이 이걸 잡았다: 읽기 경로를 지웠는데도 통과했다)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "snapshot_signals"), None)
    if fn is None or not hasattr(fn, "end_lineno"):
        return src
    lines = src.splitlines(keepends=True)
    return "".join(lines[:fn.lineno - 1] + lines[fn.end_lineno:])


def _consumer_text() -> str:
    """소비자 파일들의 **주석·독스트링·쓰기 지점을 지운** 본문."""
    out = []
    for p in _CONSUMERS:
        src = _strip_write_site(p.read_text(encoding="utf-8"))
        try:                       # 독스트링 제거 — 설명에 적힌 컬럼명이 오탐이 된다
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Module)):
                    d = ast.get_docstring(node, clean=False)
                    if d:
                        src = src.replace(d, "")
        except SyntaxError:        # 파싱 실패하면 원문 그대로 — 검사가 느슨해질 뿐 안 깨진다
            pass
        out.append(re.sub(r"^\s*#.*$", "", src, flags=re.M))
    return "\n".join(out)


def test_every_pit_column_has_a_reader():
    """**쌓기만 하고 안 보는 컬럼이 없어야 한다.** `pre_run_up_pct` 가 그랬다."""
    body = _consumer_text()
    cols = _snapshot_columns()
    assert len(cols) > 10, f"컬럼 추출이 깨졌다({len(cols)}개) — 검사가 아무것도 안 막는다"

    orphans = []
    for c in sorted(cols):
        if c in _ALLOWED:
            continue
        # 문자열 리터럴 또는 속성 접근으로 참조되면 읽은 것으로 본다.
        if re.search(rf"""["']{re.escape(c)}["']|\.{re.escape(c)}\b""", body):
            continue
        orphans.append(c)
    assert not orphans, (
        f"PIT에 쓰기만 하고 읽는 곳이 없는 컬럼: {orphans}. "
        f"소비자({[p.name for p in _CONSUMERS]})에서 읽거나, 읽지 않는 이유를 "
        f"`_ALLOWED` 에 적어라 — 쌓기만 하는 관측은 영원히 안 본다.")


def test_consumer_list_points_at_real_files():
    """소비자 파일이 사라지거나 이름이 바뀌면 그 컬럼이 전부 고아로 보인다 — 먼저 잡는다."""
    missing = [p.name for p in _CONSUMERS if not p.exists()]
    assert not missing, f"소비자 목록에 없는 파일: {missing}"


def test_the_allowlist_cannot_just_grow():
    """면제 목록이 자라면 검사가 사라진다 — 상한을 둔다."""
    assert len(_ALLOWED) <= _MAX_ALLOWED, (
        f"면제 {len(_ALLOWED)}개 > 상한 {_MAX_ALLOWED} — 컬럼을 읽거나 상한 인상을 정당화하라")
    assert all(v.strip() for v in _ALLOWED.values()), "면제에는 이유를 적는다"


def test_allowlist_has_no_ghosts():
    """사라진 컬럼이 면제 목록에 유령으로 남으면 목록이 거짓말을 한다."""
    cols = _snapshot_columns()
    ghosts = [c for c in _ALLOWED if c not in cols]
    assert not ghosts, f"스냅샷에 없는 컬럼이 면제 목록에 남아 있다: {ghosts}"


def test_it_would_have_caught_the_regression():
    """**양성 대조군** — 검사를 조일 때는 진짜로 잡는지 같이 확인한다.

    `pre_run_up_pct` 를 읽는 코드가 없던 상태를 재현해 검사가 실패하는지 본다. 안 그러면
    "아무것도 통과 못 하는 검사"와 "잘 만든 검사"를 구분할 수 없다.
    """
    body = _consumer_text()
    stripped = re.sub(r"""["']pre_run_up_pct["']|\.pre_run_up_pct\b""", "", body)
    assert not re.search(r"""["']pre_run_up_pct["']|\.pre_run_up_pct\b""", stripped)
    assert "pre_run_up_pct" in _snapshot_columns(), "회귀 재현 대상이 스냅샷에 없다"
    # 지금은 읽는 곳이 **있어야** 한다(회귀가 고쳐졌다는 뜻)
    assert re.search(r"""["']pre_run_up_pct["']|\.pre_run_up_pct\b""", body), \
        "사전 상승을 읽는 곳이 다시 사라졌다"
