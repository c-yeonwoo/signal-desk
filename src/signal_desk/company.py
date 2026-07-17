"""종목 '사업 개요'(무엇을 하는 회사) + '최근 행보'(최근 사실 2~3줄) — 초보용 맥락 소개.

맥락 소개용이지 신호가 아니다. 투자권유·전망·주가·수치·매수매도 의견은 넣지 않고 사실만 쓴다.
LLM(있으면)으로 생성해 kv에 캐시한다.
 - about: 무엇을 하는 회사(1문장). 국내는 종목명+섹터로, 해외는 AlphaVantage 영문 Description을 요약.
 - recent_moves: 최근 무엇을 했는지(제품·계약·수주·실적·투자 등) KB 원자료(뉴스+공시) 기반 2~3줄.

성능·정직 원칙:
 - 리스트/대량 경로(generate=False): LLM 호출 없음 — 캐시 또는 None(가짜 개요 금지).
 - BUY/SELL 종목 상세·해설 경로만 generate=True로 온디맨드 생성(캐시 미스 시 1회).
 - 대량 백필은 backfill*()(관리자 갱신·백그라운드 루프).
"""

from __future__ import annotations

import logging
import threading

from signal_desk import db, llm

log = logging.getLogger("signal_desk.company")

_KEY = "about:%s"
_MOVES_KEY = "moves:%s"
_about_locks: dict[str, threading.Lock] = {}
_about_locks_mu = threading.Lock()


def _about_lock(ticker: str) -> threading.Lock:
    with _about_locks_mu:
        lk = _about_locks.get(ticker)
        if lk is None:
            lk = threading.Lock()
            _about_locks[ticker] = lk
        return lk


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


def _generate(ticker: str, name: str, sector: str | None, market: str,
              us_description: str | None, *, model: str | None = None) -> str | None:
    """LLM으로 사업 개요 생성(사실 기반, 투자권유·전망·수치 금지). 실패 시 None."""
    if not llm.available():
        return None
    use_model = model or llm.DIGEST_MODEL
    quality = use_model != llm.DIGEST_MODEL
    max_chars = 220 if quality else 160
    if market == "us" and us_description:
        system = ("너는 미국 주식 소개 작가다. 아래 영문 회사 설명을 초보 투자자도 이해되게 한국어 "
                  f"{'2문장(120자 내외)' if quality else '1~2문장(80자 내외)'}으로 요약한다. "
                  "무엇을 만들고 파는 회사인지 사업 중심으로 쓰고, "
                  "투자 권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"회사: {name}({ticker})\n영문 설명:\n{us_description[:2000]}\n\n"
                'JSON으로만: {"about": "한국어 요약"}')
    elif market == "us":
        system = ("너는 미국 주식 소개 작가다. 이 종목이 '무엇을 하는 회사'인지 초보도 이해되게 한국어 "
                  f"{'1~2문장' if quality else '1문장(60자 내외)'}으로 설명한다. 아는 사실만 쓰고 모르면 섹터만 언급. "
                  "투자권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"종목: {name}({ticker}), 섹터: {sector or '미상'}\n"
                'JSON으로만: {"about": "무엇을 하는 회사인지"}')
    else:
        system = ("너는 한국 주식 소개 작가다. 이 종목이 '무엇을 하는 회사'인지 초보도 이해되게 "
                  f"{'1~2문장' if quality else '1문장(45자 내외)'}으로 설명한다. 주력 제품·서비스를 구체적으로 짚되, "
                  "아는 사실만 쓰고 모르면 섹터만 언급한다. 투자권유·전망·주가·수치는 절대 넣지 마라.")
        user = (f"종목: {name}({ticker}), 섹터: {sector or '미상'}\n"
                'JSON으로만: {"about": "무엇을 하는 회사인지"}')
    out = llm.complete_json(system, user, max_tokens=280 if quality else 200, model=use_model)
    if out and out.get("about"):
        return str(out["about"]).strip()[:max_chars]
    return None


def about(ticker: str, name: str, sector: str | None = None, market: str = "kr",
          generate: bool = False, us_description: str | None = None,
          *, model: str | None = None) -> str | None:
    """사업 개요 한 줄. 캐시 우선.
    - generate=False(기본, 요청 경로): 캐시가 있으면 캐시, 없으면 None(무비용·무LLM·무허구).
    - generate=True(백필·활성 시그널 온디맨드): 캐시 없으면 LLM 생성 후 캐시. 실패 시 None.
    - model: generate 시 사용할 모델(기본 Haiku). BUY/SELL 온디맨드는 Sonnet 등."""
    cached = _cache_get(ticker)
    if cached:
        return cached
    if not generate:
        return None
    # 상세·해설이 동시에 들어오면 동일 종목 LLM 이중 호출 방지
    with _about_lock(ticker):
        cached = _cache_get(ticker)
        if cached:
            return cached
        desc = _generate(ticker, name, sector, market, us_description, model=model)
        if desc:
            _cache_set(ticker, desc)
            return desc
    return None


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


# ---------- 최근 행보(최근 무엇을 했나) — KB 원자료(뉴스+공시) 기반 사실 요약 ----------
def _freshness_sig(ticker: str) -> str | None:
    """KB 다이제스트의 소스 수·최신시각으로 캐시 신선도 서명. 새 뉴스가 들어오면 서명이 바뀌어 재생성."""
    try:
        dg = db.kb_digest_get(ticker)
    except Exception:
        dg = None
    if not dg:
        return None
    return f"{dg.get('n_sources')}:{dg.get('newest_ts')}"


def _generate_moves(name: str, items: list[dict]) -> list[str] | None:
    """헤드라인에서 '최근 무엇을 했나'만 2~3개 짧은 불릿으로. 전망·의견 금지. 실패 시 None."""
    if not llm.available() or not items:
        return None
    lines = "\n".join(f"- [{it.get('source', '')}] {it.get('title', '')} :: {(it.get('summary') or '')[:120]}"
                      for it in items[:10])
    system = ("너는 한국 주식 뉴스 요약가다. 아래 헤드라인에서 이 회사가 '최근 무엇을 했는지'(신제품·계약·"
              "수주·실적발표·투자·인수·인사 등 사실)만 2~3개의 짧은 불릿으로 요약한다. 각 불릿 30자 내외, "
              "명사형으로. 투자권유·전망·목표주가·매수매도 의견은 절대 금지. 헤드라인에 없는 내용은 지어내지 "
              "마라. 사실이 부족하면 빈 배열을 반환한다.")
    user = (f"회사: {name}\n헤드라인:\n{lines}\n\n"
            'JSON으로만: {"moves": ["불릿1", "불릿2", "불릿3"]}')
    out = llm.complete_json(system, user, max_tokens=300, model=llm.DIGEST_MODEL)
    if out and isinstance(out.get("moves"), list):
        moves = [str(m).strip()[:60] for m in out["moves"] if str(m).strip()][:3]
        return moves or None
    return None


def recent_moves(ticker: str, name: str, generate: bool = False) -> list[str] | None:
    """최근 행보 불릿(≤3). KB 원자료(뉴스+공시) 기반 사실. 신호·전망 아님.
    - generate=False(요청 경로): 신선한 캐시가 있으면 반환, 없으면 None(무LLM).
    - generate=True(백필): 캐시가 없거나 오래됐으면 LLM 생성·캐시."""
    sig = _freshness_sig(ticker)
    try:
        cached = db.kv_get(_MOVES_KEY % ticker)
    except Exception:
        cached = None
    fresh = isinstance(cached, dict) and cached.get("moves") and (sig is None or cached.get("sig") == sig)
    if fresh:
        return cached["moves"]
    if not generate:
        # 요청 경로: 오래됐어도 있으면 보여주되, 아예 없으면 None
        return cached["moves"] if isinstance(cached, dict) and cached.get("moves") else None
    if sig is None:  # KB 문서가 없으면 생성 대상 아님(허구 방지)
        return cached["moves"] if isinstance(cached, dict) and cached.get("moves") else None
    items = db.kb_entries_recent(ticker, 12, confirmed_only=True)
    moves = _generate_moves(name, items)
    if moves:
        try:
            db.kv_set(_MOVES_KEY % ticker, {"moves": moves, "sig": sig})
        except Exception:
            pass
        return moves
    return cached["moves"] if isinstance(cached, dict) and cached.get("moves") else None


def backfill_moves(targets: list[dict], max_llm: int = 20) -> int:
    """targets: [{ticker, name}]. KB 문서가 있고 캐시가 오래된 종목만 최근 행보 재생성. 생성 수 반환."""
    if not llm.available():
        return 0
    got = 0
    for t in targets:
        tk, nm = t.get("ticker"), t.get("name", "")
        if not tk:
            continue
        sig = _freshness_sig(tk)
        if sig is None:  # KB 문서 없음 → 대상 아님
            continue
        try:
            cached = db.kv_get(_MOVES_KEY % tk)
        except Exception:
            cached = None
        if isinstance(cached, dict) and cached.get("sig") == sig and cached.get("moves"):
            continue  # 신선 → 스킵
        items = db.kb_entries_recent(tk, 12, confirmed_only=True)
        moves = _generate_moves(nm, items)
        if moves:
            try:
                db.kv_set(_MOVES_KEY % tk, {"moves": moves, "sig": sig})
            except Exception:
                pass
            got += 1
            if got >= max_llm:
                break
    return got
