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
