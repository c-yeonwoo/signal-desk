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


def alphavantage_key() -> str | None:
    return os.environ.get("ALPHAVANTAGE_API_KEY")


def naver_search() -> tuple[str | None, str | None]:
    return os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")


def youtube_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY")


def anthropic_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")
