"""소유자 전용 KIS 조회/사전검증 API. 실주문 라우트는 없다."""

from fastapi import APIRouter, Body, HTTPException, Request
from contextlib import contextmanager
import threading

from signal_desk import auth, config
from signal_desk.broker import live

router = APIRouter(prefix="/api/live", tags=["live-readiness"])
_READ_LOCK = threading.Lock()


@contextmanager
def _read_slot():
    if not _READ_LOCK.acquire(blocking=False):
        raise HTTPException(429, "계좌 조회가 진행 중입니다. 완료 후 다시 시도하세요.")
    try:
        yield
    finally:
        _READ_LOCK.release()


def _owner(request: Request) -> None:
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    owner = config.kis_account_owner()
    if not user:
        raise HTTPException(401, "인증이 필요합니다.")
    if not owner or (user.get("email") or "").strip().lower() != owner:
        raise HTTPException(403, "KIS_ACCOUNT_OWNER에 지정된 계좌 소유자만 접근할 수 있습니다.")


def _credentials(request: Request) -> dict:
    _owner(request)
    try:
        creds = config.kis_credentials()
    except ValueError:
        raise HTTPException(503, "KIS_ENV는 demo 또는 real이어야 합니다.") from None
    if not creds:
        raise HTTPException(503, "서버의 KIS 인증정보가 설정되지 않았습니다.")
    return creds


@router.get("/status")
def status(request: Request):
    _owner(request)
    return live.status()


@router.post("/account")
def account(request: Request):
    creds = _credentials(request)
    try:
        with _read_slot():
            return live.snapshot(creds)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(502, "증권사 계좌 응답 검증 실패") from None


@router.post("/preflight")
def preflight(request: Request, data: dict = Body(...)):
    creds = _credentials(request)
    try:
        with _read_slot():
            return live.preflight(data, creds)
    except ValueError:
        raise HTTPException(400, "입력 또는 증권사 응답이 유효하지 않습니다.") from None
    except (KeyError, TypeError, OverflowError):
        raise HTTPException(502, "증권사 응답 검증 실패") from None
