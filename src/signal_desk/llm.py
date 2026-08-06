"""Anthropic 메시지 API 최소 클라이언트 — 표준 라이브러리(urllib)만 사용(SDK 의존 없음).

ANTHROPIC_API_KEY가 없으면 모든 함수가 조용히 None을 반환한다(그레이스풀 폴백 — LLM 없이도
봇/시그널이 결정론적으로 계속 동작). 키·응답 본문은 로그에 남기지 않는다.

용도: 봇 의사결정 자문(signals/advisor.py), KB 다이제스트 생성(kb.py). 저빈도 호출이라
품질 우선으로 Opus를 기본 모델로 둔다.

호출마다 usage(input/output tokens)를 SQLite에 기록해 이 앱만의 추정 비용을 집계한다
(공유 API 키와 Anthropic 콘솔 청구를 분리하기 위함).
"""

from __future__ import annotations

import json
import logging
import urllib.request

from signal_desk import config, db

log = logging.getLogger("signal_desk.llm")

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"                  # 최상위 — 매수 자문·오염 검증 등 위험 판단
DIGEST_MODEL = "claude-haiku-4-5-20251001"         # 종목 다이제스트·분류·백필 about(대량·저비용)
NARRATIVE_MODEL = "claude-sonnet-5"                # 거시·후보이벤트·가설·사용자 해설(경제 함의·가독성)
# BUY/SELL 시그널 해설·온디맨드 개요 — 초보 이해도 우선(캐시됨, HOLD는 호출 안 함)
SIGNAL_EXPLAIN_MODEL = DEFAULT_MODEL
ABOUT_QUALITY_MODEL = NARRATIVE_MODEL
# 별칭 — 의도 명확화(호출부에서 DIGEST_MODEL과 혼동 방지)
CLASSIFY_MODEL = DIGEST_MODEL                     # 스코프 분류·recent_moves
DIGEST_QUALITY_MODEL = NARRATIVE_MODEL            # 거시/리포트/후보 이벤트(종목 일일 digest는 DIGEST_MODEL)
_TIMEOUT = 60

# USD / 1M tokens — Anthropic 공개 단가 근사(2026-07). 캐시·배치 할인 미반영.
# 모델 ID prefix 매칭. sonnet-5는 도입가 $2/$10 적용.
_PRICE_USD_PER_MTOK: list[tuple[str, float, float]] = [
    ("claude-opus", 5.0, 25.0),
    ("claude-sonnet-5", 2.0, 10.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 1.0, 5.0),
]
_PRICE_FALLBACK = (3.0, 15.0)  # 미매칭 시 Sonnet급


# ── 예산 게이트 ────────────────────────────────────────────────────────────────
# 상한을 **여기 한 곳**에 둔다. `llm`을 부르는 모듈이 11개(chat·kb·audit·advisor·narrative·
# hypothesis·rebalance·bot·company·shortform·api)이고, 라우트마다 상한을 걸면 새 호출자가
# 조용히 우회한다 — `shorts` 누락·`signal_config` 미주입·커버리지 게이트와 같은 병이다.
# `_post_json`이 유일한 네트워크 호출 지점이라 여기를 지나지 않고 토큰을 쓸 방법이 없다.
_DAY = 86400


def budget_state() -> dict:
    """예산 상태 — `{ok, reason, day_usd, day_cap, month_usd, month_cap}`.

    지출을 **못 읽으면 막는다**(fail-open은 게이트가 없는 것과 같다). 0.0(안 씀)과
    None(모름)을 구분하는 것이 이 판정의 전부다.
    """
    day_cap, month_cap = config.llm_daily_budget_usd(), config.llm_monthly_budget_usd()
    day, month = db.llm_spend_usd(window_sec=_DAY), db.llm_spend_usd(window_sec=30 * _DAY)
    out = {"day_usd": day, "day_cap": day_cap, "month_usd": month, "month_cap": month_cap,
           "ok": False, "reason": ""}
    if day is None or month is None:
        out["reason"] = "LLM 지출을 읽을 수 없어 호출을 보류합니다(예산 게이트 fail-closed)."
        return out
    if day_cap <= 0:
        out["reason"] = "LLM 일일 예산이 0으로 설정돼 있습니다(LLM_DAILY_BUDGET_USD)."
        return out
    if day >= day_cap:
        out["reason"] = (f"오늘 LLM 지출 ${day:.2f} / 상한 ${day_cap:.2f} — 자정(UTC 기준 24시간 창) "
                         f"이후 다시 열립니다.")
        return out
    if month >= month_cap:
        out["reason"] = f"30일 LLM 지출 ${month:.2f} / 상한 ${month_cap:.2f} — 상한을 올려야 합니다."
        return out
    out["ok"] = True
    return out


class BudgetExceeded(RuntimeError):
    """예산 상한으로 호출이 막혔다. **키 없음(None)과 구분하려고** 예외로 낸다 —
    둘이 같아 보이면 "LLM 미연동"과 "예산 초과"를 화면에서 가를 수 없다(0의 이유 규칙)."""


def available() -> bool:
    return bool(config.anthropic_key())


def price_for_model(model: str) -> tuple[float, float]:
    """(input_usd_per_mtok, output_usd_per_mtok)."""
    m = (model or "").lower()
    for prefix, inp, out in _PRICE_USD_PER_MTOK:
        if prefix in m:
            return inp, out
    return _PRICE_FALLBACK


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_r, out_r = price_for_model(model)
    return (input_tokens / 1_000_000.0) * inp_r + (output_tokens / 1_000_000.0) * out_r


def _record_usage(model: str, usage: dict | None, *, kind: str, ok: bool = True) -> None:
    if not usage:
        return
    try:
        from signal_desk import db
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        if inp <= 0 and out <= 0:
            return
        db.llm_usage_add(
            model=model, kind=kind,
            input_tokens=inp, output_tokens=out,
            cost_usd=estimate_cost_usd(model, inp, out),
            ok=ok,
        )
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
    except Exception:
        log.debug("llm usage 기록 실패", exc_info=True)


def _post_json(body: dict, *, timeout: float = _TIMEOUT) -> dict | None:
    key = config.anthropic_key()
    if not key:
        return None
    st = budget_state()
    if not st["ok"]:
        log.warning("LLM 예산 게이트 차단: %s", st["reason"])
        raise BudgetExceeded(st["reason"])
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=raw, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", _VERSION)
    req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def complete(system: str, user: str, *, max_tokens: int = 1024, model: str = DEFAULT_MODEL) -> str | None:
    """system+user 프롬프트로 1회 호출해 텍스트를 반환. 키 없거나 실패 시 None.
    (temperature는 opus-4-8에서 deprecated라 보내지 않는다)"""
    try:
        data = _post_json({
            "model": model, "max_tokens": max_tokens,
            "system": system, "messages": [{"role": "user", "content": user}],
        })
        if not data:
            return None
        _record_usage(model, data.get("usage"), kind="complete")
        parts = data.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
    except Exception as e:  # 키/본문은 로깅하지 않음
        log.warning("LLM 호출 실패: %s", type(e).__name__)
        return None


def messages_with_tools(system: str, messages: list, tools: list, *,
                        max_tokens: int = 1024, model: str = NARRATIVE_MODEL) -> dict | None:
    """tool use 지원 1회 호출. messages는 Anthropic 형식(assistant tool_use / user tool_result 포함).
    반환: {"content": [...], "stop_reason": str} 또는 None(키 없음·실패). 툴 루프는 호출측(chat.py)이 돈다."""
    try:
        data = _post_json({
            "model": model, "max_tokens": max_tokens, "system": system,
            "tools": tools, "messages": messages,
        })
        if not data:
            return None
        _record_usage(model, data.get("usage"), kind="tools")
        return {"content": data.get("content", []), "stop_reason": data.get("stop_reason")}
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
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
    # **스트리밍은 `_post_json`을 지나지 않는다** — 자기 요청을 따로 만든다. 그래서 예산 게이트를
    # `_post_json`에만 걸었을 때 `/api/chat/stream`(=막아야 할 바로 그 경로)이 통째로 우회했다.
    # "단일 호출 지점"이라는 전제를 확인하지 않으면 게이트는 있는 척만 한다.
    st = budget_state()
    if not st["ok"]:
        log.warning("LLM 예산 게이트 차단(stream): %s", st["reason"])
        raise BudgetExceeded(st["reason"])
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
    usage: dict = {}
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
                except BudgetExceeded:
                    raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
                except Exception:
                    continue
                et = ev.get("type")
                if et == "message_start":
                    msg = ev.get("message") or {}
                    if msg.get("usage"):
                        usage = {**usage, **msg["usage"]}
                elif et == "content_block_start":
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
                    if ev.get("usage"):
                        usage = {**usage, **ev["usage"]}
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
    except Exception as e:
        log.warning("LLM 스트리밍 실패: %s", type(e).__name__)
        yield ("result", None)
        return
    _record_usage(model, usage or None, kind="stream")
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("type") == "tool_use":
            try:
                inp = json.loads(b.get("_json") or "{}")
            except BudgetExceeded:
                raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
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
    kind = "document" if media_type == "application/pdf" else "image"
    content = [
        {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data_b64}},
        {"type": "text", "text": user},
    ]
    try:
        data = _post_json({
            "model": model, "max_tokens": max_tokens,
            "system": system, "messages": [{"role": "user", "content": content}],
        }, timeout=_TIMEOUT * 2)
        if not data:
            return None
        _record_usage(model, data.get("usage"), kind="vision")
        parts = data.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
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
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except BudgetExceeded:
                raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
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
    except BudgetExceeded:
        raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except BudgetExceeded:
                raise                       # 예산 차단은 키 없음(None)과 구분해 올린다
            except Exception:
                return None
    return None
