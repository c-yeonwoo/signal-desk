"""토스 국내주식 소액 파일럿의 사용자 개별 승인 HTTP 경계."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Request

from signal_desk import auth, config, notify
from signal_desk.broker import intent_ledger, toss_manual

router = APIRouter(prefix="/api/live/toss-manual", tags=["manual-toss-order"])
log = logging.getLogger("signal_desk.toss_manual_routes")


def _notify_order_state(uid: int, intent_id: str, status: str) -> None:
    """Private, idempotent state alert; notification failures never change broker semantics."""
    if status not in ("UNKNOWN", "FILLED"):
        return
    try:
        row = intent_ledger.get(intent_id)
        if not row or row["uid"] != uid:
            return
        event_at = int(row["updated"])
        label = "주문 결과 불명" if status == "UNKNOWN" else "체결 확인"
        action = "매수" if row["side"] == "BUY" else "매도"
        detail = ("토스 앱에서 주문 내역을 확인하기 전에는 절대 재제출하지 마세요."
                  if status == "UNKNOWN" else
                  "체결가는 지정가와 다를 수 있습니다. 앱에서 실제 체결 내역을 확인하세요.")
        text = (f"⚠️ 실계좌 {label} · {row['symbol']} {action} {row['quantity']}주\n"
                f"주문 상태시각 {time.strftime('%m/%d %H:%M UTC', time.gmtime(event_at))}\n{detail}")
        notify.enqueue_user(uid, text, dedupe_key=f"live-order:{intent_id}:{status}",
                            market="kr", alert_type="live_order", priority="critical",
                            expires_at=int(time.time()) + (24 * 3600 if status == "UNKNOWN" else 2 * 3600))
    except Exception as exc:  # noqa: BLE001 — account order response must be independent of Telegram
        log.warning("실주문 개인 알림 실패: %s", type(exc).__name__)


def _owner_uid(request: Request) -> int:
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    owner = config.toss_account_owner()
    if not user or not owner or (user.get("email") or "").lower() != owner:
        raise HTTPException(403, "토스 실계좌 소유자만 접근할 수 있습니다.")
    return user["id"]


def _approval_request(request: Request) -> None:
    """쿠키 기반 세션의 cross-site form POST를 막고 JSON fetch만 받는다."""
    if (request.headers.get("x-signal-desk-order") != "manual"
            or request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json"):
        raise HTTPException(403, "앱 안에서 주문 요청을 다시 시작하세요.")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.netloc != request.headers.get("host") or (config.is_prod() and parsed.scheme != "https"):
            raise HTTPException(403, "요청 출처가 현재 앱과 일치하지 않습니다.")


@router.get("/status")
def manual_status(request: Request):
    _owner_uid(request)
    return toss_manual.availability()


@router.post("/preview")
def manual_preview(request: Request, data: dict = Body(...)):
    uid = _owner_uid(request)
    _approval_request(request)
    try:
        return toss_manual.preview(uid, style=data.get("source_style"),
                                   event_id=data.get("source_event_id"),
                                   limit_price=data.get("limit_price"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/submit")
def manual_submit(request: Request, data: dict = Body(...)):
    uid = _owner_uid(request)
    _approval_request(request)
    try:
        result = toss_manual.submit(uid, intent_id=data.get("intent_id"),
                                    confirmation_phrase=data.get("confirmation_phrase"))
        _notify_order_state(uid, result["intent_id"], result["status"])
        return result
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/history")
def manual_history(request: Request):
    uid = _owner_uid(request)
    return {"orders": intent_ledger.recent(uid), "automatic_copy_enabled": False}


@router.get("/orders/{intent_id}")
def manual_order_status(request: Request, intent_id: str):
    uid = _owner_uid(request)
    try:
        result = toss_manual.order_status(uid, intent_id)
        _notify_order_state(uid, intent_id, result["status"])
        return result
    except ValueError:
        raise HTTPException(404, "주문 기록을 찾을 수 없습니다.") from None
