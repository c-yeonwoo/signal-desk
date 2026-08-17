"""런타임에 읽는 비 .py 파일이 **휠에 들어가는지** 검사한다(2026-08-17).

Dockerfile이 `pip install .`(비편집)을 쓰므로 `[tool.setuptools.package-data]` 에 선언된
것만 배포된다. 빠지면 **프로덕션에서만** 500이 난다 — 로컬은 소스 트리를 그대로 보니
테스트가 전부 통과한다.

실제로 그랬다(PWA #399): `web/icons/` 에 아이콘을 넣고 배포했더니

    /manifest.webmanifest  200      ← 라우트는 있다
    /sw.js                 200
    /icons/icon-192.png    500      ← 파일이 없다
    /icons/nope.png        404      ← 화이트리스트도 돈다

`package-data` 가 `web/*` 였는데 **`*` 는 하위 디렉토리를 안 잡는다.**

이 리포는 `src/` **밖** 파일에 대해 이미 같은 검사를 갖고 있다
(`test_runtime_read_files_outside_src_are_in_the_docker_image` — `docs/preregistered.toml` 이
이미지에 없어 판정 보드가 통째로 죽었던 사건). **`src/` 안쪽만 빠져 있었고**, 하필 거기가
`COPY src ./src` 때문에 "복사되니까 괜찮다"고 착각하기 쉬운 자리다 — 복사는 되지만
`pip install` 이 휠로 만들 때 걸러진다.

검사는 **패턴 대조**로 한다(휠을 매번 빌드하면 느리다). 대신 패턴이 실제로 맞는지는
`pip wheel` 로 한 번 확인했다: 고치기 전 `web/index.html` 하나 → 고친 뒤 아이콘 4개 포함.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PKG = _ROOT / "src" / "signal_desk"

# 휠에 없어도 되는 것 — **이유를 하나씩 적는다.**
_ALLOWED_SUFFIXES = {
    ".pyc": "빌드 산출물",
    ".pyi": "타입 스텁 — 런타임에 안 읽는다",
}
_ALLOWED_DIRS = {"__pycache__": "빌드 산출물"}


def _matches(path: str, pattern: str) -> bool:
    """setuptools 의미론의 glob 대조.

    **`fnmatch` 를 쓰면 안 된다** — `fnmatch` 의 `*` 는 `/` 를 넘어가서
    `web/*` 가 `web/icons/icon-192.png` 를 **잡는다고** 답한다. 즉 이 검사가 버그를 낸
    바로 그 설정을 통과시킨다(양성 대조군이 이걸 잡았다). setuptools 는 `glob` 규약이라
    `*` 는 한 경로 조각 안에서만 매칭된다.
    """
    rx = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            rx.append(".*")
            i += 2
        elif c == "*":
            rx.append("[^/]*")
            i += 1
        elif c == "?":
            rx.append("[^/]")
            i += 1
        else:
            rx.append(re.escape(c))
            i += 1
    return re.fullmatch("".join(rx), path) is not None


def test_the_matcher_follows_setuptools_not_fnmatch():
    """대조기 자체를 먼저 검사한다 — 여기가 틀리면 나머지가 전부 무의미하다."""
    assert _matches("web/index.html", "web/*")
    assert not _matches("web/icons/icon-192.png", "web/*"), "`*` 가 `/` 를 넘었다"
    assert _matches("web/icons/icon-192.png", "web/icons/*")
    assert _matches("web/icons/icon-192.png", "web/**")


def _package_data_patterns() -> list[str]:
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(cfg["tool"]["setuptools"]["package-data"]["signal_desk"])


def _runtime_files() -> list[Path]:
    """`src/signal_desk` 밑의 비 .py 파일(패키지 상대경로)."""
    out = []
    for p in sorted(_PKG.rglob("*")):
        if not p.is_file() or p.suffix == ".py":
            continue
        if any(part in _ALLOWED_DIRS for part in p.parts):
            continue
        if p.suffix in _ALLOWED_SUFFIXES:
            continue
        out.append(p.relative_to(_PKG))
    return out


def test_every_runtime_asset_is_declared_as_package_data():
    """**이게 그 버그다.** `web/*` 는 `web/icons/*.png` 를 안 잡는다.

    선언에서 빠지면 로컬은 전부 통과하고 프로덕션에서만 500이 난다.
    """
    pats = _package_data_patterns()
    files = _runtime_files()
    assert files, "런타임 자산이 하나도 안 잡혔다 — 검사가 아무것도 안 막는다"
    missing = [str(f) for f in files
               if not any(_matches(str(f), pat) for pat in pats)]
    assert not missing, (
        f"휠에 안 들어가는 런타임 파일: {missing}. "
        f"`[tool.setuptools.package-data] signal_desk` 에 패턴을 추가하라 "
        f"(현재: {pats}). `*` 는 하위 디렉토리를 안 잡는다.")


def test_the_pwa_icons_are_covered():
    """회귀 대상을 이름으로 박는다 — 패턴이 조용히 좁아지면 여기서 잡힌다."""
    pats = _package_data_patterns()
    for name in ("web/icons/icon-192.png", "web/icons/icon-512.png",
                 "web/icons/icon-maskable-512.png", "web/icons/icon-180.png"):
        assert any(_matches(name, p) for p in pats), f"{name} 이 안 잡힌다"


def test_it_would_have_caught_the_regression():
    """**양성 대조군** — 옛 패턴(`web/*` 만)이면 실패해야 한다.

    안 그러면 "아무것도 통과 못 하는 검사"와 "잘 만든 검사"를 구분할 수 없다.
    """
    old = ["web/*", "assets/fonts/*.ttf"]
    missed = [str(f) for f in _runtime_files()
              if not any(_matches(str(f), p) for p in old)]
    assert missed, "옛 패턴으로도 다 잡힌다면 이 검사는 아무것도 안 막는다"
    assert any("icons" in m for m in missed), missed


def test_allowlist_states_a_reason_and_cannot_just_grow():
    assert len(_ALLOWED_SUFFIXES) + len(_ALLOWED_DIRS) <= 6, "면제가 자라기만 한다"
    for d in (_ALLOWED_SUFFIXES, _ALLOWED_DIRS):
        assert all(v.strip() for v in d.values()), "면제에는 이유를 적는다"


def test_the_installed_package_can_actually_find_them():
    """설치된 위치에서 **실제로 열린다.** 선언만 맞고 경로가 틀리면 여전히 500이다."""
    from signal_desk import api
    for name in api._PWA_ICONS:
        p = api.WEB_DIR / "icons" / name
        assert p.exists() and p.stat().st_size > 1000, f"{p} 를 못 읽는다"
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 이 PNG가 아니다"


def test_python_version_floor_matches_the_dockerfile():
    """휠이 만들어져도 런타임 파이썬이 다르면 다른 것이 깨진다 — 같이 본다."""
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    req = cfg["project"]["requires-python"]
    docker = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.1" in docker, "베이스 이미지가 파이썬이 아니다"
    ver = docker.split("python:")[1].split("-")[0]
    major, minor = (int(x) for x in ver.split(".")[:2])
    assert (major, minor) >= (3, 11), f"Dockerfile 파이썬 {ver} — 3.14는 금지(pandas 세그폴트)"
    assert (major, minor) < (3, 14), f"Dockerfile 파이썬 {ver} — 3.14 금지(CLAUDE.md)"
    assert req, "requires-python 이 비어 있다"
    assert sys.version_info[:2] >= (3, 11)
