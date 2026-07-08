"""숏폼 영상 렌더 — 장면 SVG(→PNG, cairosvg) + 나레이션(Typecast TTS) → ffmpeg로 세로 mp4.

파이프라인(draft 1건):
  장면마다  ① narration → Typecast mp3(없으면 무음)  ② SVG → PNG(cairosvg, 번들 나눔고딕)
           ③ ffmpeg: PNG(오디오 길이만큼 정지) + mp3 → 클립
  전체     ④ 클립 concat → 1080x1920 mp4 → data/cache/shortform_video/{sid}.mp4

의존: cairosvg(pip) + ffmpeg(시스템). 한글은 번들 폰트(assets/fonts/NanumGothic*)를 fontconfig로
등록해 시스템 무관하게 렌더. 도구·키 없으면 명확한 사유로 실패(그레이스풀).
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from signal_desk import db, store
from signal_desk.ingest import typecast

log = logging.getLogger("signal_desk.shortform_render")

_FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_W, _H = 1080, 1920
_font_ready = False


def available() -> tuple[bool, str]:
    """렌더 가능 여부 — (ok, 사유). cairosvg·ffmpeg 둘 다 있어야 함."""
    try:
        import cairosvg  # noqa: F401
    except Exception:
        return False, "cairosvg 미설치(pip install cairosvg)"
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg 미설치(시스템)"
    return True, ""


def _ensure_fonts() -> None:
    """번들 한글 폰트를 fontconfig에 등록(1회). FONTCONFIG_FILE은 cairo 로드 전에 설정해야 하지만
    cairosvg를 렌더 시점에 처음 import하므로 여기서 설정하면 유효하다."""
    global _font_ready
    if _font_ready:
        return
    cache = tempfile.mkdtemp(prefix="sdfc_")
    conf = (f'<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
            f'<fontconfig><dir>{_FONTS_DIR.resolve()}</dir><cachedir>{cache}</cachedir></fontconfig>')
    cf = os.path.join(cache, "fonts.conf")
    with open(cf, "w") as fh:
        fh.write(conf)
    os.environ.setdefault("FONTCONFIG_FILE", cf)
    _font_ready = True


def _bg_bytes(url: str) -> bytes:
    """배경 <image> 원본 바이트 — 업로드분(/api/…background-image)은 로컬 파일, 외부 http(s)는 직접.
    cairosvg가 url_fetcher를 안 받는 버전이라 렌더 전에 data URI로 인라인하기 위함. 실패 시 빈 바이트."""
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
    data URI로 인라인(해소 실패 시 단색 rect로 대체)."""
    import base64
    import re
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


def _scene_png(svg: str, path: str) -> None:
    import cairosvg
    cairosvg.svg2png(bytestring=_render_svg(svg).encode("utf-8"), write_to=path,
                     output_width=_W, output_height=_H)


_ERR_KW = ("error", "invalid", "cannot", "failed", "no such", "killed", "conversion failed",
           "unable", "not found", "permission", "out of memory", "no space")


def _run(cmd: list[str]) -> None:
    """ffmpeg/ffprobe 실행. 실패 시 리턴코드 + '진짜 에러 라인'(키워드 우선)으로 예외.
    rc가 음수/137이면 시그널 종료(예: -9 OOM kill) — 정상 stderr 없이 죽은 것."""
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").replace("\r", "\n")
        lines = [ln.strip() for ln in err.splitlines() if ln.strip() and not ln.startswith("frame=")]
        hits = [ln for ln in lines if any(k in ln.lower() for k in _ERR_KW)]
        picked = hits[-3:] if hits else lines[-4:]
        note = " (시그널 종료=리소스 부족(OOM)/타임아웃 가능)" if p.returncode < 0 or p.returncode == 137 else ""
        raise RuntimeError(f"rc={p.returncode}{note} · " + " | ".join(picked)[:420])


def _audio_duration(path: str) -> float | None:
    """오디오 길이(초). 못 읽거나 0이면 None — 빈/깨진 mp3에 -shortest가 걸려 0프레임 나는 것 방지."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.strip()
        d = float(out) if out else 0.0
        return d if d > 0 else None
    except Exception:
        return None


def _scene_clip(png: str, audio: str | None, dur: float, out: str) -> bool:
    """장면 1개 → mp4 클립. 유효 오디오(≥0.3s)면 그 길이, 아니면 dur초 무음. 모든 클립을 30fps·
    44.1kHz·스테레오로 통일해 concat이 항상 되게 한다. 반환: 실제 오디오 사용 여부."""
    adur = _audio_duration(audio) if audio else None
    use_audio = bool(adur and adur >= 0.3)
    # ultrafast preset — 저사양 컨테이너(Railway)에서 CPU/메모리 부담·시간 대폭 감소(정지 이미지라 화질 무난).
    common = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
              "-vf", f"scale={_W}:{_H}", "-r", "30",
              "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
    if use_audio:
        cmd = (["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png, "-i", audio]
               + common + ["-tune", "stillimage", "-t", f"{adur:.2f}", "-shortest", out])
    else:  # 무음 — anullsrc로 오디오 스트림은 두되(스트림 구성 통일) dur초
        cmd = (["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
               + common + ["-t", f"{dur:.2f}", out])
    _run(cmd)
    return use_audio


def render(sid: str) -> dict:
    """draft 1건 → mp4. 반환 {ok, url|reason, scenes, has_audio}. 사람이 검수 후 발행."""
    ok, why = available()
    if not ok:
        return {"ok": False, "reason": why}
    item = db.shortform_get(sid)
    if not item or not item.get("scenes"):
        return {"ok": False, "reason": "장면이 없는 초안(재생성 필요)"}
    _ensure_fonts()
    scenes = item["scenes"]
    tts_on = typecast.available()
    tmp = tempfile.mkdtemp(prefix="sfvid_")
    clips, has_audio = [], False
    try:
        for i, sc in enumerate(scenes):
            png = os.path.join(tmp, f"s{i}.png")
            _scene_png(sc.get("svg") or "", png)
            audio = None
            if tts_on and sc.get("narration"):
                mp3 = typecast.synthesize(sc["narration"])
                if mp3 and len(mp3) > 500:  # 너무 작으면 유효 오디오 아님 → 무음 폴백
                    audio = os.path.join(tmp, f"s{i}.mp3")
                    with open(audio, "wb") as fh:
                        fh.write(mp3)
            clip = os.path.join(tmp, f"c{i}.mp4")
            if _scene_clip(png, audio, float(sc.get("dur") or 3.0), clip):  # 실제 오디오 사용됨
                has_audio = True
            clips.append(clip)
        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w") as fh:
            fh.write("".join(f"file '{c}'\n" for c in clips))
        out = os.path.join(tmp, f"{sid}.mp4")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing,
              "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100",
              "-ac", "2", "-r", "30", "-pix_fmt", "yuv420p", out])
        # 볼륨에 저장하지 않고 바이트로 반환 → 엔드포인트가 스트리밍 후 임시파일과 함께 폐기(볼륨 사용 0).
        with open(out, "rb") as fh:
            data = fh.read()
        return {"ok": True, "data": data, "scenes": len(scenes), "has_audio": has_audio}
    except Exception as e:
        log.warning("숏폼 렌더 실패(%s): %s", sid, e)
        return {"ok": False, "reason": f"렌더 실패: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # PNG·mp3·클립·최종 mp4 전부 즉시 삭제


# ---------- 로컬 렌더 자료(zip) — 저사양 서버(Railway OOM) 대신 사용자 PC에서 렌더 ----------
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
import base64, json, os, pathlib, subprocess, tempfile, urllib.request

HERE = pathlib.Path(__file__).parent
# 번들 나눔고딕을 fontconfig에 등록(한글)
_cache = tempfile.mkdtemp()
_cf = os.path.join(_cache, "fonts.conf")
open(_cf, "w").write('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
                     '<fontconfig><dir>%s</dir><cachedir>%s</cachedir></fontconfig>' % (HERE, _cache))
os.environ["FONTCONFIG_FILE"] = _cf
import cairosvg  # noqa: E402

KEY = os.environ.get("TYPECAST_API_KEY")
VOICE = os.environ.get("TYPECAST_VOICE_ID", "tc_5eb55cf1f0b0a700071f89c7")


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


def export_bundle(sid: str) -> bytes | None:
    """로컬 렌더용 zip 바이트 — 렌더 준비된 장면 SVG(폰트·크기·배경 인라인) + 나레이션/길이 +
    번들 폰트 + 자기완결 render.py + README. 서버는 rasterize/ffmpeg 안 함(OOM 회피)."""
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
    return buf.getvalue()
