"""화면 함수가 정의만 되고 **호출되지 않으면** 잡는다(2026-08-17).

두 개가 남아 있었다.

- `climatePill` — #305(시그널 탭 정보 밀도 축약)가 호출부 2곳을 **의도적으로** 지웠는데
  함수·CSS(`.clim-pill` 8줄)·`_CLIM_TIP` 상수가 남았다. 더 나쁜 것은 `test_smoke` 가
  `"climatePill" in html` 을 검사하고 있어 **문자열이 남아 있는 한 통과**했다는 점이다 —
  기능이 1년 가까이 "있는 것처럼" 보였다.
- `whyHoldNote` — `holdNote` 로 대체됐는데 옛 구현이 남았다. 둘 다 HOLD 사유를 그리므로
  나중에 문구를 고칠 때 어느 쪽을 고쳐야 하는지 알 수 없다.

이 리포는 라우트·푸시 헬퍼·PIT 컬럼에 대해 이미 같은 검사를 갖고 있다. 화면 함수만 빠져 있었고,
하필 거기가 "지웠다고 생각한 것"이 남는 자리다.

**존재를 문자열로 검사하지 않는다** — 호출부가 사라져도 통과하기 때문이다. 닿을 수 있는지를
검사한다.
"""

from __future__ import annotations

import re
from pathlib import Path

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"

# 호출부가 없어도 되는 함수 — **이유를 하나씩 적는다.**
_ALLOWED = {
    "_refreshGuard": "beforeunload 리스너로 등록된다(호출이 아니라 전달)",
    "auditItem": "`items.map(auditItem)` 으로 전달된다",
}
_MAX_ALLOWED = 6


def _body() -> str:
    """`//` 줄 주석만 지운 본문 — 설명에 적힌 함수명이 오탐이 되기 때문이다.

    **`/* */` 는 지우지 않는다.** DOTALL 로 지우면 JS 문자열·정규식 안의 `*/` 와 잘못 짝지어져
    본문이 통째로 사라진다(실측: 61,865자 · `onclick` 175 → 128개가 증발해 멀쩡한 함수 29개가
    죽은 것으로 잡혔다). 남은 CSS 주석은 함수명을 거의 안 담으므로 오탐 위험이 훨씬 작다 —
    **검사가 조용히 느슨해지는 쪽보다 조금 시끄러운 쪽**을 고른다.
    """
    src = _HTML.read_text(encoding="utf-8")
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _declared(body: str) -> set[str]:
    """**함수 선언만** 센다 — `(async function init(){…})()` 같은 즉시실행 표현식은 이름이
    있어도 호출부가 `})()` 라 "안 불린다"로 잡힌다(실제로 `init` 이 오탐이었다).
    앞 글자가 `(`·`=`·`,`·`:` 면 표현식이므로 뺀다 — `async` 는 패턴이 흡수하므로
    `(async function init(` 도 `(` 로 판정된다(이걸 빠뜨려 한 번 더 오탐이 났다).
    """
    out = set()
    for m in re.finditer(r"(.)\s*(?:async\s+)?\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", body):
        if m.group(1) not in "(=,:":   # `async` 는 아래 패턴이 이미 흡수한다
            out.add(m.group(2))
    return out


def _dead() -> list[str]:
    body = _body()
    out = []
    for name in sorted(_declared(body)):
        if name in _ALLOWED:
            continue
        esc = re.escape(name)
        # 호출: `name(` 이 정의 말고 또 있는가. 전달: `map(name)`·`onclick="name(...)"`·리스너
        calls = len(re.findall(rf"(?<![\w$.]){esc}\s*\(", body))
        passed = len(re.findall(rf"[(,]\s*{esc}\s*[,)]|['\"]{esc}\b", body))
        if calls <= 1 and passed == 0:
            out.append(name)
    return out


def test_no_function_is_defined_without_being_reachable():
    """**지웠다고 생각한 것이 남는 자리다.** 죽은 함수는 죽은 CSS·상수를 데리고 다닌다."""
    dead = _dead()
    assert not dead, (
        f"정의만 있고 닿지 않는 화면 함수: {dead}. 호출부를 붙이거나, 함수·CSS·상수를 함께 "
        f"지우거나, 닿지 않아도 되는 이유를 `_ALLOWED` 에 적어라.")


def test_the_allowlist_cannot_just_grow():
    assert len(_ALLOWED) <= _MAX_ALLOWED, f"면제 {len(_ALLOWED)} > 상한 {_MAX_ALLOWED}"
    assert all(v.strip() for v in _ALLOWED.values()), "면제에는 이유를 적는다"


def test_allowlist_has_no_ghosts():
    """사라진 함수가 면제 목록에 남으면 목록이 거짓말을 한다."""
    body = _body()
    ghosts = [n for n in _ALLOWED if n not in _declared(body)]
    assert not ghosts, f"없는 함수가 면제 목록에 남아 있다: {ghosts}"


def test_removed_feature_left_no_css_or_constants():
    """죽은 함수만 지우고 CSS·상수를 남기면 다음 사람이 기능이 있는 줄 안다."""
    src = _HTML.read_text(encoding="utf-8")
    for residue in ("clim-pill", "_CLIM_TIP", "climatePill", "whyHoldNote"):
        assert residue not in src, f"제거된 기능의 잔재가 남아 있다: {residue}"


def test_it_would_have_caught_the_regression():
    """**양성 대조군** — 검사를 조일 때는 진짜로 잡는지 같이 확인한다.

    `climatePill` 이 그랬듯, 정의만 있고 호출이 없는 함수를 넣으면 잡혀야 한다.
    """
    body = _body() + "\nfunction __zzz_probe_never_called(x){ return x; }\n"
    calls = len(re.findall(r"(?<![\w$.])__zzz_probe_never_called\s*\(", body))
    passed = len(re.findall(r"[(,]\s*__zzz_probe_never_called\s*[,)]|['\"]__zzz_probe_never_called\b", body))
    assert calls == 1 and passed == 0, "탐지 규칙이 정의 1회·전달 0회를 못 센다"
