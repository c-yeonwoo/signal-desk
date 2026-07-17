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
DEFAULT_MODEL = "claude-opus-4-8"                  # 최상위 — 매수 자문·오염 검증 등 위험 판단
DIGEST_MODEL = "claude-haiku-4-5-20251001"         # KB 다이제스트·요약(정형·대량) — 저비용·고속
NARRATIVE_MODEL = "claude-sonnet-5"                # 사용자용 해설(캐시됨) — 가독성·뉘앙스
# BUY/SELL 시그널 해설·온디맨드 개요 — 초보 이해도 우선(캐시됨, HOLD는 호출 안 함)
SIGNAL_EXPLAIN_MODEL = DEFAULT_MODEL
ABOUT_QUALITY_MODEL = NARRATIVE_MODEL
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


def messages_with_tools(system: str, messages: list, tools: list, *,
                        max_tokens: int = 1024, model: str = NARRATIVE_MODEL) -> dict | None:
    """tool use 지원 1회 호출. messages는 Anthropic 형식(assistant tool_use / user tool_result 포함).
    반환: {"content": [...], "stop_reason": str} 또는 None(키 없음·실패). 툴 루프는 호출측(chat.py)이 돈다."""
    key = config.anthropic_key()
    if not key:
        return None
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "tools": tools, "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", _VERSION)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"content": data.get("content", []), "stop_reason": data.get("stop_reason")}
    except Exception as e:
        log.warning("LLM tools 호출 실패: %s", type(e).__name__)
        return None


def stream_call(system: str, messages: list, tools: list, *,
                max_tokens: int = 1200, model: str = NARRATIVE_MODEL):
    """tool use + 토큰 스트리밍 1회 호출(제너레이터). SSE를 파싱해:
      ('text', 델타)  — 텍스트 토큰이 생성될 때마다
      ('result', {content, stop_reason})  — 마지막에 1회(블록 재구성 완료; 실패·키없음이면 None)
    를 yield한다. 툴 루프는 chat.answer_stream이 이 제너레이터를 소비하며 돈다."""
    key = config.anthropic_key()
    if not key:
        yield ("result", None)
        return
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "tools": tools, "messages": messages, "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", _VERSION)
    req.add_header("content-type", "application/json")
    blocks: dict[int, dict] = {}
    stop_reason = None
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            for raw in resp:                       # 응답을 라인 단위 스트림으로 소비
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                et = ev.get("type")
                if et == "content_block_start":
                    blocks[ev["index"]] = {**(ev.get("content_block") or {}), "_json": ""}
                elif et == "content_block_delta":
                    d = ev.get("delta") or {}
                    b = blocks.setdefault(ev["index"], {"type": "text", "text": "", "_json": ""})
                    if d.get("type") == "text_delta":
                        b["text"] = b.get("text", "") + d.get("text", "")
                        yield ("text", d.get("text", ""))
                    elif d.get("type") == "input_json_delta":
                        b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                elif et == "message_delta":
                    stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
    except Exception as e:
        log.warning("LLM 스트리밍 실패: %s", type(e).__name__)
        yield ("result", None)
        return
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("type") == "tool_use":
            try:
                inp = json.loads(b.get("_json") or "{}")
            except Exception:
                inp = {}
            content.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": inp})
        elif b.get("type") == "text":
            content.append({"type": "text", "text": b.get("text", "")})
    yield ("result", {"content": content, "stop_reason": stop_reason})


def complete_vision(system: str, user: str, *, media_type: str, data_b64: str,
                    max_tokens: int = 1500, model: str = DEFAULT_MODEL) -> str | None:
    """PDF/이미지를 첨부해 1회 호출(멀티모달) — 스캔 문서·이미지 OCR을 별도 엔진 없이 모델이 직접 인식.
    media_type: 'application/pdf' 또는 'image/png'|'image/jpeg' 등. 키 없거나 실패 시 None."""
    key = config.anthropic_key()
    if not key:
        return None
    kind = "document" if media_type == "application/pdf" else "image"
    content = [
        {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data_b64}},
        {"type": "text", "text": user},
    ]
    body = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system, "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", _VERSION)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT * 2) as resp:  # 문서 인식은 더 오래 걸림
            data = json.loads(resp.read().decode("utf-8"))
        parts = data.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None
    except Exception as e:
        log.warning("LLM vision 호출 실패: %s", type(e).__name__)
        return None


def complete_json_vision(system: str, user: str, *, media_type: str, data_b64: str,
                         max_tokens: int = 1500, model: str = DEFAULT_MODEL) -> dict | None:
    """complete_vision + JSON 강제·관대 파싱."""
    sys_json = system + "\n\n반드시 유효한 JSON 하나만 출력하라. 설명·코드펜스 없이 JSON 객체만."
    text = complete_vision(sys_json, user, media_type=media_type, data_b64=data_b64,
                           max_tokens=max_tokens, model=model)
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
