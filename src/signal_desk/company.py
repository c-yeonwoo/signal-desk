"""종목 '사업 개요'(무엇을 하는 회사) — 초보용 한 줄 소개.

맥락 소개용이지 신호가 아니다. 투자권유·전망·주가·수치는 넣지 않고, 사실 기반으로만 쓴다.
LLM(있으면)으로 1~2문장을 생성해 kv에 캐시(종목별 1회). 국내는 종목명+섹터로, 해외는
AlphaVantage 영문 Description을 한국어로 요약한다. LLM 미설정·실패 시 섹터 폴백.

성능 원칙: 시그널 렌더(요청 경로)에서는 절대 LLM을 호출하지 않는다(수백 종목 동기 호출 방지).
요청 경로는 `about(generate=False)`로 캐시 또는 섹터 폴백만 반환하고, 실제 LLM 생성은
`backfill()`(관리자 갱신·백그라운드 루프)에서 증분으로 채운다.
"""

from __future__ import annotations

import logging

from signal_desk import db, llm

log = logging.getLogger("signal_desk.company")

_KEY = "about:%s"


def _cache_get(ticker: str) -> str | None:
    try:
        v = db.kv_get(_KEY % ticker)
        return v if isinstance(v, str) and v.strip() else None
    except Exception:
        return None


def _cache_set(ticker: str, v: str) -> None:
    try:
        db.kv_set(_KEY % ticker, v)
    except Exception:
        pass


def _fallback(sector: str | None, market: str) -> str:
    if sector:
        return f"{sector} 분야의 기업입니다."
    return "미국 상장 기업입니다." if market == "us" else "국내 상장 기업입니다."


def _generate(ticker: str, name: str, sector: str | None, market: str,
              us_description: str | None) -> str | None:
    """LLM으로 사업 개요 한 줄 생성(사실 기반, 투자권유·전망·수치 금지). 실패 시 None."""
    if not llm.available():
        return None
    if market == "us" and us_description:
        system = ("너는 미국 주식 소개 작가다. 아래 영문 회사 설명을 초보 투자자도 이해되게 한국어 "
                  "1~2문장(80자 내외)으로 요약한다. 무엇을 만들고 파는 회사인지 사업 중심으로 쓰고, "
                  "투자 권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"회사: {name}({ticker})\n영문 설명:\n{us_description[:2000]}\n\n"
                'JSON으로만: {"about": "한국어 1~2문장 요약"}')
    elif market == "us":
        system = ("너는 미국 주식 소개 작가다. 이 종목이 '무엇을 하는 회사'인지 초보도 이해되게 한국어 "
                  "1문장(60자 내외)으로 설명한다. 아는 사실만 쓰고 모르면 섹터만 언급. "
                  "투자권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"종목: {name}({ticker}), 섹터: {sector or '미상'}\n"
                'JSON으로만: {"about": "무엇을 하는 회사인지 한 문장"}')
    else:
        system = ("너는 한국 주식 소개 작가다. 이 종목이 '무엇을 하는 회사'인지 초보도 이해되게 1문장"
                  "(45자 내외)으로 설명한다. 아는 사실만 쓰고 모르면 섹터만 언급. "
                  "투자권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"종목: {name}({ticker}), 섹터: {sector or '미상'}\n"
                'JSON으로만: {"about": "무엇을 하는 회사인지 한 문장"}')
    out = llm.complete_json(system, user, max_tokens=200, model=llm.DIGEST_MODEL)
    if out and out.get("about"):
        return str(out["about"]).strip()[:160]
    return None


def about(ticker: str, name: str, sector: str | None = None, market: str = "kr",
          generate: bool = False, us_description: str | None = None) -> str:
    """사업 개요 한 줄. 캐시 우선.
    - generate=False(기본, 요청 경로): 캐시가 있으면 캐시, 없으면 섹터 폴백(무비용·무LLM).
    - generate=True(백필 경로): 캐시 없으면 LLM 생성 후 캐시. 실패 시 폴백."""
    cached = _cache_get(ticker)
    if cached:
        return cached
    if generate:
        desc = _generate(ticker, name, sector, market, us_description)
        if desc:
            _cache_set(ticker, desc)
            return desc
    return _fallback(sector, market)


def backfill(targets: list[dict], max_llm: int = 40) -> int:
    """targets: [{ticker, name, sector, market, us_description?}]. 아직 캐시 없는 종목만 LLM 생성·캐시.
    비용·속도 상한(max_llm)까지만 채우고 나머지는 다음 호출에서 이어감. 생성 수 반환. LLM 없으면 0."""
    if not llm.available():
        return 0
    got = 0
    for t in targets:
        tk = t.get("ticker")
        if not tk or _cache_get(tk):
            continue
        desc = _generate(tk, t.get("name", ""), t.get("sector"),
                         t.get("market", "kr"), t.get("us_description"))
        if desc:
            _cache_set(tk, desc)
            got += 1
            if got >= max_llm:
                break
    return got
