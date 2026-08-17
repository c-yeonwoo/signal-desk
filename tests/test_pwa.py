"""홈 화면 아이콘(PWA) — 모바일에서 앱처럼 열기(2026-08-17).

앱은 이미 배포돼 모바일 브라우저로 열린다. 여기서 더하는 것은 **껍데기**뿐이다 —
아이콘·전체화면·주소창 색. 기능은 하나도 안 바뀐다.

## 캐시하지 않는다

서비스워커는 Android 설치 조건(fetch 핸들러가 있는 워커)만 만족시키고 **아무 것도
저장하지 않는다**. 이 앱의 숫자는 낡으면 위험하다 — 오프라인에서 어제 매수 신호를 보여주는
것보다 안 열리는 게 낫다("실패한 조회는 낡은 값을 남기지 않는다" · "정지는 조용하다").

## 공개 경로여야 한다

`/api/*` 는 인증 미들웨어가 **라우팅 전에** 401을 낸다. manifest·아이콘·워커는 설치 시점에
로그인 전일 수 있으므로 `/api/` 밖에 두어야 한다 — 안 그러면 설치가 조용히 실패한다.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_WEB = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web"
_HTML = _WEB / "index.html"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api
    importlib.reload(api)
    return TestClient(api.app)


def test_manifest_is_public_and_installable(client):
    """`/api/*` 안에 두면 인증 미들웨어가 401을 내 설치가 조용히 실패한다."""
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200, "manifest가 안 열린다 — 로그인 전에도 공개여야 한다"
    assert "manifest" in r.headers["content-type"]
    m = r.json()
    # 설치 가능 최소 조건(Chrome): name/short_name · start_url · display · 192+512 아이콘
    assert m["name"] and m["short_name"] and m["start_url"] and m["display"] == "standalone"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes, f"설치 조건 아이콘이 없다: {sizes}"
    assert any(i.get("purpose") == "maskable" for i in m["icons"]), \
        "maskable이 없으면 안드로이드에서 아이콘이 흰 원 안에 축소돼 박힌다"


def test_icons_exist_and_are_served(client):
    for i in client.get("/manifest.webmanifest").json()["icons"]:
        r = client.get(i["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", i["src"]
        assert len(r.content) > 1000, f"{i['src']} 가 너무 작다 — 빈 파일일 수 있다"
    r = client.get("/icons/icon-180.png")          # apple-touch-icon (manifest 밖)
    assert r.status_code == 200


def test_unknown_icon_names_are_refused(client):
    """목록에 없는 이름은 404 — 경로로 다른 파일을 읽히지 않게."""
    for bad in ("nope.png", "../index.html", "..%2Findex.html"):
        assert client.get(f"/icons/{bad}").status_code in (404, 400), bad


def test_service_worker_caches_nothing(client):
    """**낡은 시그널을 보여주는 것보다 안 열리는 게 낫다.** 캐시 API를 쓰면 안 된다."""
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    js = r.text
    assert "addEventListener('fetch'" in js, "fetch 핸들러가 없으면 안드로이드가 설치를 제안하지 않는다"
    for banned in ("caches.open", "cache.put", "cache.addAll", "CacheStorage"):
        assert banned not in js, f"서비스워커가 캐시한다({banned}) — 낡은 숫자가 화면에 남는다"


def test_head_has_the_ios_path_too(client):
    """iOS는 manifest가 아니라 `apple-touch-icon` 을 본다 — 하나만 넣으면 반쪽이다."""
    html = _HTML.read_text(encoding="utf-8")
    head = html[:html.index("</head>")]
    assert '<link rel="manifest"' in head
    assert 'rel="apple-touch-icon"' in head, "iOS 홈 화면 아이콘이 없다"
    assert 'name="apple-mobile-web-app-capable"' in head, "iOS에서 주소창이 남는다"
    assert 'name="theme-color"' in head


def test_theme_color_matches_the_icon_background():
    """주소창 색과 아이콘 배경이 다르면 설치 직후 이음매가 보인다."""
    html = _HTML.read_text(encoding="utf-8")
    theme = re.search(r'name="theme-color" content="(#[0-9A-Fa-f]{6})"', html).group(1)
    api_src = (Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "api.py"
               ).read_text(encoding="utf-8")
    server = re.search(r'_PWA_THEME = "(#[0-9A-Fa-f]{6})"', api_src).group(1)
    assert theme.upper() == server.upper(), f"화면 {theme} vs 서버 {server} — 두 곳이 갈라졌다"
    # favicon(인라인 SVG)의 배경과도 같아야 한다 — 세 곳이 같은 값이어야 한다
    assert theme.replace("#", "%23").upper() in html.upper(), "favicon 배경과 다르다"


def test_registration_failure_does_not_break_the_app():
    """서비스워커 등록이 실패해도 앱은 그대로 돌아야 한다."""
    html = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    i = html.index("serviceWorker.register")
    assert ".catch(" in html[i:i + 120], "등록 실패가 앱을 멈춘다"


def test_icons_are_inside_src_so_the_docker_image_has_them():
    """런타임에 읽는 파일이 배포 이미지에 있어야 한다 — `COPY src ./src` 안에 두는 이유."""
    files = sorted(p.name for p in (_WEB / "icons").glob("*.png"))
    assert files, "아이콘 파일이 없다"
    docker = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY src ./src" in docker, "web/icons 를 이미지에 넣는 COPY가 없다"


def test_manifest_start_url_lands_on_the_signal_tab(client):
    """홈 화면에서 열면 첫 화면이 시그널이어야 한다 — 설치해 놓고 매번 탭을 옮기면 안 쓴다."""
    assert client.get("/manifest.webmanifest").json()["start_url"].endswith("#signal")
