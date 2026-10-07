"""실제 봇 판단의 소규모 자동 보존. 주문과 연구 판정에는 관여하지 않는다."""

from __future__ import annotations

import logging
import shutil
import time
import uuid

from signal_desk import db
from signal_desk.signals import decision_snapshot

log = logging.getLogger("signal_desk.decision_capture_pilot")

_MIB = 1024 * 1024
_MAX_RAW_INPUT = 64 * _MIB
_MAX_STORED = 32 * _MIB
_MIN_FREE = 128 * _MIB


def storage_budget_status() -> dict:
    """관리자 화면의 실제 마운트 여유와 자동 캡처 예산(원문·계좌 제외)."""
    try:
        usage = shutil.disk_usage(db.DB.parent)
        stored = sum(int(row["stored_bytes"] or 0) for row in db.decision_artifact_storage())
        return {"available": True, "volume_total_bytes": usage.total,
                "volume_free_bytes": usage.free, "artifact_stored_bytes": stored,
                "artifact_cap_bytes": _MAX_STORED,
                "can_capture": (usage.free >= max(_MIN_FREE, int(usage.total * 0.20))
                                and stored < _MAX_STORED)}
    except OSError:
        return {"available": False, "can_capture": False}


def _budget_reason(market: str, price_bundle: tuple, capture: dict) -> tuple[str | None, dict | None]:
    budget = storage_budget_status()
    if not budget["available"] or budget["volume_free_bytes"] < max(
            _MIN_FREE, int(budget["volume_total_bytes"] * 0.20)):
        return "volume_free_low", None
    if budget["artifact_stored_bytes"] >= _MAX_STORED:
        return "artifact_budget_reached", None
    # 게이트의 closes_by/dates_by는 이미 가격 조각에 있으므로 두 번 세지 않는다.
    # 실제 저장과 동일한 다섯 payload를 압축해 상한을 보수적으로 계산한다.
    size = decision_snapshot.estimate_captured_decision(market, price_bundle, capture)
    if size["raw_bytes"] > _MAX_RAW_INPUT:
        return "input_too_large", size
    if budget["artifact_stored_bytes"] + size["stored_bytes"] > _MAX_STORED:
        return "artifact_budget_reached", size
    if budget["volume_free_bytes"] - size["stored_bytes"] < max(
            _MIN_FREE, int(budget["volume_total_bytes"] * 0.20)):
        return "volume_free_low", size
    return None, size


def capture_once(market: str, session: str, price_bundle: tuple, capture: dict) -> dict:
    """시장·거래일당 첫 정규 봇 실행 한 번만 저장·즉시 재생한다.

    실패도 같은 날의 결과로 남겨 무제한 재시도/볼륨 증가를 막는다.
    호출자는 예외가 주문으로 전파되지 않게 이 함수를 격리한다.
    """
    owner = uuid.uuid4().hex
    now = int(time.time())
    if not db.decision_pilot_claim(market, session, owner, now=now,
                                   allow_size_retry=True):
        return {"status": "already_claimed"}
    try:
        reason, size = _budget_reason(market, price_bundle, capture)
        size_fields = ({"estimated_raw_bytes": size["raw_bytes"],
                        "estimated_stored_bytes": size["stored_bytes"]} if size else {})
        if reason:
            state = {"status": "skipped", "reason": reason, **size_fields}
        else:
            started = time.perf_counter()
            before_bytes = sum(int(row["stored_bytes"] or 0)
                               for row in db.decision_artifact_storage())
            refs = decision_snapshot.persist_captured_decision(
                market, price_bundle, capture, observed_at=now)
            replay = decision_snapshot.replay_signal_decision(market, refs["signal_output_id"])
            after_bytes = sum(int(row["stored_bytes"] or 0)
                              for row in db.decision_artifact_storage())
            state = {"status": "saved" if replay["match"] else "failed",
                     "reason": None if replay["match"] else "replay_mismatch",
                     "signal_output_id": refs["signal_output_id"],
                     "replay_match": bool(replay["match"]),
                     "elapsed_ms": round((time.perf_counter() - started) * 1000),
                     "artifact_bytes_added": max(0, after_bytes - before_bytes),
                     **size_fields}
    except Exception as exc:  # noqa: BLE001 — 계좌 실행과 감사용 보존은 분리한다
        log.warning("판단 자동 보존 실패 (%s/%s): %s", market, session, type(exc).__name__)
        state = {"status": "failed", "reason": type(exc).__name__}
    db.decision_pilot_finish(market, session, owner, state, now=int(time.time()))
    return state


def record_unavailable(market: str, session: str, reason: str) -> None:
    """캡처 세대가 달라 저장할 수 없었던 첫 실행도 조용한 0이 되지 않게 한다."""
    if reason not in ("generation_changed", "capture_missing"):
        raise ValueError("invalid unavailable reason")
    owner = uuid.uuid4().hex
    now = int(time.time())
    if db.decision_pilot_claim(market, session, owner, now=now):
        db.decision_pilot_finish(market, session, owner,
                                 {"status": "skipped", "reason": reason}, now=now)
