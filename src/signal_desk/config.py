"""환경변수 로더 — .env 의 API 키를 os.environ 으로 (python-dotenv 미사용, 의존 최소화)."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def is_prod() -> bool:
    """배포 환경 여부. 로컬 dev는 APP_ENV 가 없음."""
    return os.environ.get("APP_ENV") == "prod"


def krx_key() -> str | None:
    return os.environ.get("KRX_API_KEY")


def kis_credentials() -> dict | None:
    """KIS 자동매매봇 인증정보. 하나라도 없으면 None(그레이스풀 폴백).

    KIS_ENV는 반드시 'demo'(모의투자)여야 주문이 안전 — 'prod'면 실계좌 주문 API를 호출한다."""
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    account_no = os.environ.get("KIS_ACCOUNT_NO")
    if not (app_key and app_secret and account_no):
        return None
    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "account_no": account_no,
        "product_cd": os.environ.get("KIS_ACCOUNT_PRODUCT_CD", "01"),
        "env": os.environ.get("KIS_ENV", "demo"),
    }


def dart_key() -> str | None:
    return os.environ.get("DART_API_KEY")


def ecos_key() -> str | None:
    """한국은행 ECOS(기준금리·거시지표) API 키."""
    return os.environ.get("ECOS_API_KEY")


def fred_key() -> str | None:
    """FRED(미 세인트루이스 연은) API 키 — CPI/기준금리/국채금리/나스닥 등 거시 시황 지표."""
    return os.environ.get("FRED_API_KEY")


def alphavantage_key() -> str | None:
    return os.environ.get("ALPHAVANTAGE_API_KEY")


def naver_search() -> tuple[str | None, str | None]:
    return os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")


def youtube_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY")


def anthropic_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def fanding_cookie() -> str | None:
    """fanding.kr 앱 REST 인증 쿠키(device_uid + tt). 세션 토큰이라 만료 시 .env 갱신 필요.
    미설정이면 None(자동수집 스킵)."""
    tt = os.environ.get("FANDING_TT")
    if not tt:
        return None
    dev = os.environ.get("FANDING_DEVICE_UID", "")
    return f"device_uid={dev}; tt={tt}"


def outstanding_authors() -> list[str]:
    """아웃스탠딩(outstanding.kr) 수집 대상 작가 화이트리스트(login_id). 신뢰 전문가만 골라
    기고를 수집한다(오염 방지). OUTSTANDING_AUTHORS(.env, 콤마구분) + 기본 oky97(오건영·매크로)."""
    raw = os.environ.get("OUTSTANDING_AUTHORS", "")
    ids = [a.strip() for a in raw.split(",") if a.strip()]
    return ids or ["oky97"]


def outstanding_cookie() -> str | None:
    """아웃스탠딩 로그인 쿠키 — 유료(isPrivate) 기고 본문 조회용(세션 토큰, 만료 시 갱신).
    공개 기고는 쿠키 없이 수집되므로 미설정이면 유료글만 건너뛴다."""
    return os.environ.get("OUTSTANDING_COOKIE") or None


def youtube_channels() -> list[str]:
    """유튜브 KB 수집 대상 채널 화이트리스트(핸들, @ 없이). 신뢰 채널만 수집(오염 방지).
    YOUTUBE_CHANNELS(.env, 콤마구분) + 기본 sbs_explained(교양이를 부탁해·거시/자산시장 해설)."""
    raw = os.environ.get("YOUTUBE_CHANNELS", "")
    ids = [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]
    return ids or ["sbs_explained"]


def broker_backend() -> str:
    """국내 자동매매 브로커 백엔드 — 'kis'(모의투자 실계좌) 또는 'paper'(자체 모의계좌).
    BROKER_BACKEND(.env) 우선, 미설정 시 KIS 자격증명 있으면 kis, 없으면 paper.
    KIS 비표준 포트(29443/9443)가 막힌 환경에선 BROKER_BACKEND=paper로 자체 모의계좌 사용."""
    v = (os.environ.get("BROKER_BACKEND") or "").strip().lower()
    if v in ("kis", "paper"):
        return v
    return "kis" if kis_credentials() else "paper"


def paper_seed_cash() -> float:
    """자체 모의계좌 초기 자본(원). PAPER_SEED_CASH(.env) 또는 기본 1,000만원."""
    try:
        return float(os.environ.get("PAPER_SEED_CASH", "10000000"))
    except ValueError:
        return 10_000_000.0


def bot_kill_switch() -> bool:
    """긴급 정지 — BOT_KILL_SWITCH가 켜져 있으면 자동매매봇이 어떤 주문도 내지 않는다(하드 스톱)."""
    return (os.environ.get("BOT_KILL_SWITCH", "") or "").strip().lower() in ("1", "true", "on", "yes")


def bot_daily_loss_limit_pct() -> float:
    """장중 일일 손실 한도(양수 비율). 당일 시작 평가액 대비 이 % 넘게 하락하면 신규 매수를 멈춘다
    (리스크 청산 매도는 계속). BOT_DAILY_LOSS_LIMIT_PCT(.env), 기본 8%."""
    try:
        return abs(float(os.environ.get("BOT_DAILY_LOSS_LIMIT_PCT", "0.08")))
    except ValueError:
        return 0.08


def allow_real_orders() -> bool:
    """실계좌(KIS_ENV!='demo') 실주문 허용 여부 — 명시적으로 ALLOW_REAL_ORDERS를 켜야만 True.
    실수로 실계좌에 주문 나가는 것을 막는 이중 안전장치(기본 False → 실계좌면 주문 거부)."""
    return (os.environ.get("ALLOW_REAL_ORDERS", "") or "").strip().lower() in ("1", "true", "on", "yes")


def bot_run_interval_minutes() -> int:
    """자동매매봇 백그라운드 루프 실행 간격(분). 기본 5분(장중 5분마다 시그널 점검·매매)."""
    return int(os.environ.get("BOT_RUN_INTERVAL_MINUTES", "5"))


def admin_emails() -> set[str]:
    """관리자 화이트리스트(소문자). ADMIN_EMAILS(.env, 콤마구분) + 데모 계정 기본 포함.
    관리자만 엔진 설정·KB 적재·데이터 갱신 등 관리 기능에 접근한다."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    emails.add("devcheck@example.com")  # 데모/개발 계정
    return emails


def is_admin(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in admin_emails()
