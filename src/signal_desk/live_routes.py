"""소유자 전용 KIS 조회/사전검증 API. 실주문 라우트는 없다."""

from fastapi import APIRouter, Body, HTTPException, Request
from contextlib import contextmanager
import threading

from signal_desk import auth, bot, config, db, strategy
from signal_desk.broker import live
from signal_desk.signals import advisor_shadow

router = APIRouter(prefix="/api/live", tags=["live-readiness"])
_READ_LOCK = threading.Lock()
_COPY_EVENT_MAX_AGE_SEC = 15 * 60


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


@router.get("/copy-policy")
def copy_policy_get(request: Request):
    _owner(request)
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    return db.live_copy_policy_get(user["id"])


@router.put("/copy-policy")
def copy_policy_put(request: Request, data: dict = Body(...)):
    _owner(request)
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    try:
        return db.live_copy_policy_set(user["id"], data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _copy_source(style: str, event_id: object) -> dict:
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise HTTPException(400, "참조 봇 거래 번호가 필요합니다.")
    if style not in strategy.STYLES:
        raise HTTPException(400, "안정형·균형형·공격형 중 하나를 선택하세요.")
    bot.ensure_reference_bots()
    uid = next(uid for uid, name in bot.REFERENCE_BOTS.items() if name == style)
    source = db.bot_trade_get(uid, event_id, "kr")
    if source is None:
        raise HTTPException(404, "선택한 참조 봇 거래를 찾을 수 없습니다.")
    return source


def _source_safety(style: str) -> dict:
    try:
        return advisor_shadow.decision_status(
            style=style, summary=advisor_shadow.cached_summary())
    except Exception:
        # 상태를 읽을 수 없는데 '정상'이라고 보이는 것이 가장 위험하다. 봇 본체와 같은 fail-closed.
        return {
            "style": style, "active": False, "selector_active": False,
            "buy_path_active": False, "effect": "gate_error", "fallback": "abstain",
            "source": "gate_error",
            "reason": "advisor 안전 게이트 상태를 확인하지 못해 신규 매수를 보류합니다.",
        }


@router.get("/source-safety")
def source_safety(style: str = "balanced"):
    """참조 봇의 자문 실행 효과. 계좌·주문 정보가 아니므로 로그인 사용자에게 읽기 허용."""
    style = str(style or "balanced")
    if style not in strategy.STYLES:
        raise HTTPException(400, "안정형·균형형·공격형 중 하나를 선택하세요.")
    return {"style": style, "source_safety": _source_safety(style),
            "mode": "copy_preview_only", "order_transmission_enabled": False}


@router.get("/copy-events")
def copy_events(request: Request, style: str = "balanced"):
    _owner(request)
    style = str(style or "balanced")
    if style not in strategy.STYLES:
        raise HTTPException(400, "안정형·균형형·공격형 중 하나를 선택하세요.")
    bot.ensure_reference_bots()
    uid = next(uid for uid, name in bot.REFERENCE_BOTS.items() if name == style)
    return {"style": style, "events": db.bot_trades_recent(uid, 20, "kr"),
            "source_safety": _source_safety(style),
            "mode": "copy_preview_only", "order_transmission_enabled": False}


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


@router.post("/copy-preview")
def copy_preview(request: Request, data: dict = Body(...)):
    creds = _credentials(request)
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    policy = db.live_copy_policy_get(user["id"])
    if not policy["configured"]:
        raise HTTPException(409, "먼저 실계좌 추종 한도를 저장하세요.")
    source_style = data.get("source_style")
    if source_style != policy["source_style"]:
        raise HTTPException(409, "저장된 추종 성향과 선택한 참조 봇이 다릅니다.")
    source = _copy_source(source_style, data.get("source_event_id"))
    try:
        limit_price = data.get("limit_price")
        if isinstance(limit_price, bool) or not isinstance(limit_price, int) or not 0 < limit_price <= 100_000_000:
            raise ValueError("양의 정수 지정가가 필요합니다.")
        # A historical paper fill is evidence, not a fresh executable signal. Stop before broker reads.
        import time
        if not isinstance(source.get("ts"), int) or time.time() - source["ts"] > _COPY_EVENT_MAX_AGE_SEC:
            return {"ready": False, "mode": "copy_preview_only", "order_transmission_enabled": False,
                    "source": source, "policy": policy,
                    "reason": "참조 봇 거래가 15분을 넘었습니다. 과거 체결은 복사 주문 후보가 아닙니다."}
        with _read_slot():
            return live.copy_preview(source, limit_price=limit_price, policy=policy, creds=creds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except (KeyError, TypeError, OverflowError):
        raise HTTPException(502, "증권사 응답 검증 실패") from None
