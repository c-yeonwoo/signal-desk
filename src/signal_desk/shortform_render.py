"""숏폼 로컬 렌더 자료(zip) export — 저사양 서버(Railway)는 ffmpeg 렌더에서 OOM(rc=-9)이 나므로,
서버는 렌더하지 않고 렌더에 필요한 자료만 zip으로 만들어 준다. 실제 렌더(SVG→PNG + TTS + ffmpeg)는
사용자 PC에서 동봉된 render.py로 수행한다.

zip 내용: 렌더 준비된 장면 SVG(폰트·크기·배경 인라인) + scenes.json(나레이션·길이) + 번들 나눔고딕
+ 자기완결 render.py + README. 서버는 텍스트 zip만 만들어 rasterize/ffmpeg를 전혀 안 하므로 OOM 없음.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from pathlib import Path

from signal_desk import db, store

_FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_W, _H = 1080, 1920


def _bg_bytes(url: str) -> bytes:
    """배경 <image> 원본 바이트 — 업로드분(/api/…background-image)은 로컬 파일, 외부 http(s)는 직접.
    렌더용 SVG에 data URI로 인라인하기 위함(자기완결 zip). 실패 시 빈 바이트."""
    try:
        if "background-image" in url:
            p = store.shortform_bg_path()
            return p.read_bytes() if p else b""
        if url.startswith(("http://", "https://")):
            import urllib.request
            with urllib.request.urlopen(url, timeout=15) as r:
                return r.read()
    except Exception:
        return b""
    return b""


def _render_svg(svg: str) -> str:
    """장면 SVG를 래스터화용으로 변환 — ① 반응형 style→고정 크기 ② 한글 폰트 강제 ③ 배경 <image>를
    data URI로 인라인(해소 실패 시 단색 rect로 대체). 결과는 로컬 render.py가 cairosvg로 PNG화."""
    svg = svg.replace('style="width:100%;height:auto;display:block"', f'width="{_W}" height="{_H}"')

    def repl(m: "re.Match") -> str:
        tag = m.group(0)
        href = re.search(r'href="([^"]+)"', tag)
        data = _bg_bytes(href.group(1)) if href else b""
        if not data:
            return f'<rect width="{_W}" height="{_H}" fill="#0b1220"/>'  # 배경 못 받으면 단색
        uri = "data:image/png;base64," + base64.b64encode(data).decode()
        t = re.sub(r'href="[^"]+"', f'href="{uri}"', tag)
        return re.sub(r'xlink:href="[^"]+"', f'xlink:href="{uri}"', t)

    svg = re.sub(r'<image [^>]*/>', repl, svg, count=1)
    style = "<style>text,tspan{font-family:'NanumGothic';}</style>"
    return svg.replace(">", ">" + style, 1)  # 여는 <svg ...> 바로 뒤에 삽입


# ---------- 로컬 렌더 자료(zip) ----------
_LOCAL_README = """숏폼 로컬 렌더
==============
저사양 서버(Railway)는 ffmpeg 렌더에서 메모리 부족(OOM)이 나서, 이 자료를 받아 PC에서 렌더합니다.

준비물: Python + ffmpeg(brew install ffmpeg) + cairosvg(pip install cairosvg)
(폰트는 이 폴더의 NanumGothic이 자동 사용됩니다.)

실행:  python render.py
결과:  output.mp4 (세로 1080x1920)

나레이션(TTS)까지 넣으려면 실행 전 환경변수:
  export TYPECAST_API_KEY=... (선택: TYPECAST_VOICE_ID)
키가 없으면 무음 영상이 나옵니다.
"""

# render.py — 자기완결 스크립트(signal_desk 미의존). {i:02d}/{c} 등 중괄호 때문에 f-string 아님.
_LOCAL_RENDER_SCRIPT = r'''#!/usr/bin/env python3
"""숏폼 로컬 렌더 — 장면 SVG(cairosvg→PNG) + 나레이션(Typecast, 선택) → ffmpeg mp4. README 참고."""
import json, os, pathlib, subprocess, tempfile, urllib.request

HERE = pathlib.Path(__file__).parent
# 번들 나눔고딕을 fontconfig에 등록(한글)
_cache = tempfile.mkdtemp()
_cf = os.path.join(_cache, "fonts.conf")
open(_cf, "w").write('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
                     '<fontconfig><dir>%s</dir><cachedir>%s</cachedir></fontconfig>' % (HERE, _cache))
os.environ["FONTCONFIG_FILE"] = _cf
import cairosvg  # noqa: E402

KEY = os.environ.get("TYPECAST_API_KEY")
VOICE = os.environ.get("TYPECAST_VOICE_ID", "tc_6059dad0b83880769a50502f")


def tts(text):
    if not KEY or not text.strip():
        return None
    body = json.dumps({"voice_id": VOICE, "text": text[:2000], "model": "ssfm-v30",
                       "language": "kor", "output": {"audio_format": "mp3"}}).encode()
    req = urllib.request.Request("https://api.typecast.ai/v1/text-to-speech", data=body,
                                 method="POST", headers={"X-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:
        print("  (TTS 실패, 무음으로:", e, ")")
        return None


COMMON = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          "-vf", "scale=1080:1920", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
scenes = json.load(open(HERE / "scenes.json", encoding="utf-8"))
tmp = tempfile.mkdtemp()
clips = []
for sc in scenes:
    i = sc["i"]
    print("장면 %d/%d: %s" % (i + 1, len(scenes), sc.get("label", "")))
    svg = (HERE / ("scenes/%02d.svg" % i)).read_text(encoding="utf-8")
    png = os.path.join(tmp, "%d.png" % i)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=png, output_width=1080, output_height=1920)
    mp3 = tts(sc.get("narration", ""))
    clip = os.path.join(tmp, "c%d.mp4" % i)
    if mp3 and len(mp3) > 500:
        audio = os.path.join(tmp, "%d.mp3" % i)
        open(audio, "wb").write(mp3)
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png, "-i", audio]
                       + COMMON + ["-tune", "stillimage", "-shortest", clip], check=True, capture_output=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png,
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
                       + COMMON + ["-t", "%.2f" % float(sc.get("dur", 3.0)), clip], check=True, capture_output=True)
    clips.append(clip)
lst = os.path.join(tmp, "list.txt")
open(lst, "w").write("".join("file '%s'\n" % c for c in clips))
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-r", "30",
                "-pix_fmt", "yuv420p", "output.mp4"], check=True, capture_output=True)
print("완료 → output.mp4")
'''


def _safe_name(s: str) -> str:
    """파일명 안전화 — 경로 구분자·제어문자 제거(한글은 유지)."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(s or "")).strip().replace(" ", "")
    return s or "shortform"


def export(sid: str) -> tuple[bytes, str] | None:
    """로컬 렌더용 (zip 바이트, 파일명) — 렌더 준비 SVG + 나레이션/길이 + 번들 폰트 + render.py + README.
    파일명은 종목명_종목코드.zip. 없는 draft면 None."""
    item = db.shortform_get(sid)
    if not item or not item.get("scenes"):
        return None
    scenes = item["scenes"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        meta = []
        for i, sc in enumerate(scenes):
            z.writestr(f"scenes/{i:02d}.svg", _render_svg(sc.get("svg") or ""))  # 배경 인라인·폰트·고정크기
            meta.append({"i": i, "label": sc.get("label"),
                         "narration": sc.get("narration", ""), "dur": sc.get("dur", 3.0)})
        z.writestr("scenes.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for f in ("NanumGothic-Regular.ttf", "NanumGothic-Bold.ttf"):
            fp = _FONTS_DIR / f
            if fp.exists():
                z.write(fp, f)
        z.writestr("render.py", _LOCAL_RENDER_SCRIPT)
        z.writestr("README.txt", _LOCAL_README)
    fname = f"{_safe_name(item.get('name'))}_{_safe_name(item.get('ticker'))}.zip"
    return buf.getvalue(), fname
