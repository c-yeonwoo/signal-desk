"""Anthropic 메시지 API 최소 클라이언트 — 표준 라이브러리(urllib)만 사용(SDK 의존 없음).

ANTHROPIC_API_KEY가 없으면 모든 함수가 조용히 None을 반환한다(그레이스풀 폴백 — LLM 없이도
봇/시그널이 결정론적으로 계속 동작). 키·응답 본문은 로그에 남기지 않는다.

용도: 봇 의사결정 자문(signals/advisor.py), KB 다이제스트 생성(kb.py). 저빈도 호출이라
품질 우선으로 Opus를 기본 모델로 둔다.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.llm")

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"
_TIMEOUT = 60


def available() -> bool:
    return bool(config.anthropic_key())


def complete(system: str, user: str, *, max_tokens: int = 1024, model: str = DEFAULT_MODEL) -> str | None:
    """system+user 프롬프트로 1회 호출해 텍스트를 반환. 키 없거나 실패 시 None.
    (temperature는 opus-4-8에서 deprecated라 보내지 않는다)"""
    key = config.anthropic_key()
    if not key:
        return None
    body = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system, "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", _VERSION)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parts = data.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None
    except Exception as e:  # 키/본문은 로깅하지 않음
        log.warning("LLM 호출 실패: %s", type(e).__name__)
        return None


def complete_json(system: str, user: str, *, max_tokens: int = 1024, model: str = DEFAULT_MODEL) -> dict | None:
    """JSON 응답을 강제·파싱. 코드펜스/잡텍스트가 섞여도 첫 {..} 블록을 관대하게 파싱. 실패 시 None."""
    sys_json = system + "\n\n반드시 유효한 JSON 하나만 출력하라. 설명·코드펜스 없이 JSON 객체만."
    text = complete(sys_json, user, max_tokens=max_tokens, model=model)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return None
    return None
