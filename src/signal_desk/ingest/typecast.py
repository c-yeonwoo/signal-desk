"""Typecast TTS — 장면 나레이션 텍스트 → 음성(mp3 bytes). 숏폼 영상 렌더의 오디오 소스.

API: POST https://api.typecast.ai/v1/text-to-speech, 헤더 X-API-KEY, 응답은 오디오 바이트 직접.
키는 .env의 TYPECAST_API_KEY(gitignore). 키 없거나 실패 시 None(그레이스풀 — 렌더는 무음/스킵).
표준 라이브러리(urllib)만 사용(추가 의존성 없음, 레포 컨벤션).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.typecast")

_URL = "https://api.typecast.ai/v1/text-to-speech"
_TIMEOUT = 60
_MAX_CHARS = 2000  # API 상한


def available() -> bool:
    return bool(config.typecast_key())


def synthesize(text: str, voice_id: str | None = None, language: str = "kor",
               audio_format: str = "mp3") -> bytes | None:
    """텍스트 → 음성 바이트(mp3 기본). 키 없음/빈 텍스트/실패 시 None. 2000자 초과는 잘라서 요청."""
    key = config.typecast_key()
    text = (text or "").strip()
    if not key or not text:
        return None
    body = json.dumps({
        "voice_id": voice_id or config.typecast_voice_id(),
        "text": text[:_MAX_CHARS],
        "model": config.typecast_model(),
        "language": language,
        "output": {"audio_format": audio_format},
    }).encode("utf-8")
    req = urllib.request.Request(_URL, data=body, method="POST", headers={
        "X-API-KEY": key, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        log.warning("Typecast TTS 실패: HTTP %s %s", e.code, detail)
        return None
    except Exception as e:
        log.warning("Typecast TTS 실패: %s", type(e).__name__)
        return None
