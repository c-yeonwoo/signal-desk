"""Short-lived, account-scoped exact-input cache for validated advisor responses.

Only the hash of prompts/evidence is stored; errors and malformed answers never
become reusable decisions. Cache failure falls back to the existing provider path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from signal_desk import db

log = logging.getLogger("signal_desk.advisor_cache")

VERSION = "advisor-exact-input-v1"
TTL_SECONDS = 15 * 60


@lru_cache(maxsize=1)
def _source_version() -> str:
    base = Path(__file__).parent
    return hashlib.sha256((base / "advisor.py").read_bytes() + Path(__file__).read_bytes()).hexdigest()[:24]


def input_hash(*, stage: str, scope: dict, system: str, user: str,
               model: str, max_tokens: int) -> str:
    material = {"version": VERSION, "source": _source_version(), "stage": stage,
                "scope": scope, "system": system,
                "user": user, "model": model, "max_tokens": max_tokens}
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _minimal(stage: str, response: dict) -> dict:
    # Keep only fields actually consumed downstream, never arbitrary model output.
    if stage == "primary":
        return {"picks": [{"ticker": p["ticker"], "rationale": str(p.get("rationale", ""))[:200]}
                          for p in response["picks"]]}
    if stage == "challenger":
        return {"veto": [{"ticker": item["ticker"]} for item in response["veto"]]}
    raise ValueError("unknown advisor cache stage")


def call(*, stage: str, scope: dict | None, system: str, user: str,
         model: str, max_tokens: int, invoke: Callable[[], dict | None],
         valid: Callable[[dict | None], bool]) -> dict | None:
    """A cache hit reuses an already validated response; no scope means no caching.

    The caller must include uid, market, style, trade date, and policy identities
    in scope. Exact prompt text catches changed candidates/context/KB. Hits cannot
    bypass the advisor kill gate because this function is reached only after it.
    """
    if scope is None:
        return invoke()
    required = {"uid", "market", "style", "trade_date", "signal_policy_id", "execution_policy_id"}
    if not required.issubset(scope) or not scope["uid"] or not scope["trade_date"]:
        return invoke()  # incomplete provenance must not cache a trading decision
    try:
        key = input_hash(stage=stage, scope=scope, system=system, user=user,
                         model=model, max_tokens=max_tokens)
    except (TypeError, ValueError):
        return invoke()
    start = time.monotonic()
    now = int(time.time())
    try:
        saved = db.advisor_prompt_get(key, now=now)
        if valid(saved):
            _event(scope, stage, key, "hit", start)
            return saved
    except Exception as exc:
        log.warning("advisor 캐시 조회 실패(%s) — 원래 호출 경로 유지", type(exc).__name__)
    try:
        result = invoke()
    except Exception:
        _event(scope, stage, key, "provider_error", start)
        raise
    outcome = "miss_invalid"
    if valid(result):
        try:
            db.advisor_prompt_put(key, _minimal(stage, result), now=now, expires=now + TTL_SECONDS)
            outcome = "miss_saved"
        except Exception as exc:
            log.warning("advisor 캐시 저장 실패(%s) — 응답은 그대로 사용", type(exc).__name__)
            outcome = "miss_uncached"
    _event(scope, stage, key, outcome, start)
    return result


def _event(scope: dict, stage: str, key: str, outcome: str, start: float) -> None:
    try:
        db.advisor_prompt_event_add(uid=int(scope["uid"]), market=str(scope["market"]),
                                    style=str(scope["style"]), stage=stage, input_hash=key,
                                    outcome=outcome,
                                    latency_ms=max(0, round((time.monotonic() - start) * 1000)))
    except Exception:
        log.warning("advisor 캐시 계측 실패 — 매매 판단은 계속 진행")
