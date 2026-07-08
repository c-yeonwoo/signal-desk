"""숏폼 영상 렌더 — SVG 변환(폰트·크기·배경 인라인) + (도구 있으면) 실제 mp4 생성."""

import shutil

import pytest

from signal_desk import shortform, shortform_render


def test_render_svg_transform_sets_size_and_font():
    svg = shortform._intro_svg("삼성전자", "005930", "BUY", 1.8, "반도체")
    out = shortform_render._render_svg(svg)
    assert 'width="1080" height="1920"' in out          # 반응형 style → 고정 크기
    assert "font-family:'NanumGothic'" in out           # 한글 폰트 강제
    assert "width:100%" not in out


def test_render_svg_inlines_background(monkeypatch):
    # 배경 URL을 data URI로 인라인(cairosvg가 외부 fetch 못 하므로).
    monkeypatch.setattr(shortform_render, "_bg_bytes", lambda url: b"\x89PNGdummy")
    svg = shortform._intro_svg("삼성", "005930", "BUY", 1.8, "반도체", bg="https://x/bg.jpg")
    out = shortform_render._render_svg(svg)
    assert "data:image/png;base64," in out and "https://x/bg.jpg" not in out


def test_render_svg_bg_fallback_solid(monkeypatch):
    monkeypatch.setattr(shortform_render, "_bg_bytes", lambda url: b"")  # 배경 못 받으면
    svg = shortform._intro_svg("삼성", "005930", "BUY", 1.8, "반도체", bg="https://x/bg.jpg")
    out = shortform_render._render_svg(svg)
    assert '<rect width="1080" height="1920" fill="#0b1220"/>' in out  # 단색 대체


def test_fonts_bundled():
    assert (shortform_render._FONTS_DIR / "NanumGothic-Regular.ttf").exists()


_HAVE_TOOLS = shortform_render.available()[0] and shutil.which("ffmpeg")


@pytest.mark.skipif(not _HAVE_TOOLS, reason="cairosvg/ffmpeg 필요")
def test_full_render_produces_mp4(tmp_path, monkeypatch):
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    scenes = shortform._scenes_for("삼성전자", "005930", "STRONG_BUY", 2.31,
                                   ["[기술] 골든크로스", "[저평가] PER 하위"], "반도체",
                                   closes=[100.0 + i for i in range(30)])
    db.shortform_add({"id": "vt", "ticker": "005930", "name": "삼성전자", "kind": "STRONG_BUY",
                      "score": 2.31, "scenes": scenes, "card_svg": scenes[0]["svg"]})
    out = shortform_render.render("vt")
    assert out["ok"] and out["scenes"] == len(scenes) and out["has_audio"] is False
    assert len(out["data"]) > 10000 and out["data"][4:8] == b"ftyp"  # mp4 바이트 반환(볼륨 미저장)


def _real_mp3(tmp_path, sec=1.6) -> bytes:
    import subprocess
    f = tmp_path / "sine.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={sec}",
                    "-b:a", "160k", str(f)], capture_output=True)
    return f.read_bytes()


@pytest.mark.skipif(not _HAVE_TOOLS, reason="cairosvg/ffmpeg 필요")
def test_render_with_audio(tmp_path, monkeypatch):
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    mp3 = _real_mp3(tmp_path)
    monkeypatch.setattr(shortform_render.typecast, "available", lambda: True)
    monkeypatch.setattr(shortform_render.typecast, "synthesize", lambda *a, **k: mp3)
    scenes = shortform._scenes_for("삼성전자", "005930", "BUY", 1.8, ["[기술] 골든크로스"], "반도체",
                                   closes=[100.0 + i for i in range(30)])
    db.shortform_add({"id": "va", "ticker": "005930", "name": "삼성전자", "kind": "BUY",
                      "score": 1.8, "scenes": scenes, "card_svg": scenes[0]["svg"]})
    out = shortform_render.render("va")
    assert out["ok"] and out["has_audio"] is True          # 실제 오디오 결합
    assert len(out["data"]) > 10000


@pytest.mark.skipif(not _HAVE_TOOLS, reason="cairosvg/ffmpeg 필요")
def test_render_bad_audio_falls_back_silent(tmp_path, monkeypatch):
    # 빈/깨진 mp3(작은 바이트)면 무음으로 폴백 — -shortest가 0프레임 나는 사고 방지.
    from signal_desk import db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(shortform_render.typecast, "available", lambda: True)
    monkeypatch.setattr(shortform_render.typecast, "synthesize", lambda *a, **k: b"garbage")
    scenes = shortform._scenes_for("삼성전자", "005930", "BUY", 1.8, ["[기술] 골든크로스"], "반도체",
                                   closes=[100.0 + i for i in range(30)])
    db.shortform_add({"id": "vb", "ticker": "005930", "name": "삼성전자", "kind": "BUY",
                      "score": 1.8, "scenes": scenes, "card_svg": scenes[0]["svg"]})
    out = shortform_render.render("vb")
    assert out["ok"] and out["has_audio"] is False         # 무음 폴백, 그래도 mp4 생성
    assert len(out["data"]) > 10000
