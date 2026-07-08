"""숏폼 로컬 렌더 자료(export) — SVG 변환(폰트·크기·배경 인라인) + zip 번들(종목명_코드.zip).
서버 렌더는 제거됨(저사양 OOM) — 실제 mp4는 동봉 render.py로 PC에서 생성."""

import io
import zipfile

from signal_desk import shortform, shortform_render


def test_render_svg_transform_sets_size_and_font():
    svg = shortform._intro_svg("삼성전자", "005930", "BUY", 1.8, "반도체")
    out = shortform_render._render_svg(svg)
    assert 'width="1080" height="1920"' in out          # 반응형 style → 고정 크기
    assert "font-family:'NanumGothic'" in out           # 한글 폰트 강제
    assert "width:100%" not in out


def test_render_svg_inlines_background(monkeypatch):
    monkeypatch.setattr(shortform_render, "_bg_bytes", lambda url: b"\x89PNGdummy")
    svg = shortform._intro_svg("삼성", "005930", "BUY", 1.8, "반도체", bg="https://x/bg.jpg")
    out = shortform_render._render_svg(svg)
    assert "data:image/png;base64," in out and "https://x/bg.jpg" not in out


def test_render_svg_bg_fallback_solid(monkeypatch):
    monkeypatch.setattr(shortform_render, "_bg_bytes", lambda url: b"")
    svg = shortform._intro_svg("삼성", "005930", "BUY", 1.8, "반도체", bg="https://x/bg.jpg")
    out = shortform_render._render_svg(svg)
    assert '<rect width="1080" height="1920" fill="#0b1220"/>' in out


def test_fonts_bundled():
    assert (shortform_render._FONTS_DIR / "NanumGothic-Regular.ttf").exists()


def test_export_zip_contents_and_filename(tmp_path, monkeypatch):
    # zip에 장면 SVG·나레이션·폰트·render.py·README가 다 들어가고, 파일명은 종목명_코드.zip.
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    scenes = shortform._scenes_for("삼성전자", "005930", "BUY", 1.8, ["[기술] 골든크로스"], "반도체",
                                   closes=[100.0 + i for i in range(30)])
    db.shortform_add({"id": "ex", "ticker": "005930", "name": "삼성전자", "kind": "BUY",
                      "score": 1.8, "scenes": scenes, "card_svg": scenes[0]["svg"]})
    data, fname = shortform_render.export("ex")
    assert fname == "삼성전자_005930.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert {"render.py", "scenes.json", "README.txt", "NanumGothic-Regular.ttf"} <= set(names)
    assert sum(1 for n in names if n.startswith("scenes/") and n.endswith(".svg")) == len(scenes)
    # 동봉 render.py의 기본 voiceId가 신규 값인지
    script = zipfile.ZipFile(io.BytesIO(data)).read("render.py").decode()
    assert "tc_6059dad0b83880769a50502f" in script
    assert shortform_render.export("nope") is None       # 없는 draft


def test_safe_name_strips_unsafe():
    assert shortform_render._safe_name("삼성 전자/A:B") == "삼성전자AB"
    assert shortform_render._safe_name("") == "shortform"
