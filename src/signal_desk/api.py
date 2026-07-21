"""FastAPI 백엔드 — 인증/온보딩/워치리스트, 시그널/밸류에이션/국면 실데이터, SPA 서빙.

1단계 스캐폴딩 범위였던 스텁 라우트 중 후보(candidates)/매크로/AI리포트는 아직 스키마만
확정한 스텁으로 남아 있고(phase3~6), 실제 계산 로직은 signals/, ingest/에서 채워 나간다.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, Request
from fastapi import File as FastFile
from fastapi import Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from signal_desk import (
    auth, bot, brain, brain_proposals, chat, company, config, db, kb, kb_search,
    notify, shortform, signalcfg, store, strategy,
)
from signal_desk.reference import (cycle, etfs as etfs_ref, glossary, guru_screens, gurus as gurus_ref,
                                    quant_methods, sectors, us_ko, valuechain)
from signal_desk.signals import (
    accuracy, climate, hypothesis, macro, narrative, opportunity, rebalance, regime,
    regime_zone, relative, scenario, target, valuation,
)
from signal_desk.signals.engine import (
    SignalConfig, _price_only_components, backtest_summary, chart_scores_and_zones, combine,
    compute_indicator_series, evaluate, factor_contribution, walk_forward,
)

config.load_env()

log = logging.getLogger("signal_desk")

WEB_DIR = Path(__file__).parent / "web"

# 인증 게이트: /api/* 는 세션 필수(아래 prefix 만 예외). 그 외(/, 정적)는 허용.
_OPEN_PREFIXES = ("/api/auth/",)


def _uid(request: Request):
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    return u["id"] if u else None


def _kst_today() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _daily_kb_collect():
    """외부 KB 소스(미주은·오건영·유튜브·해외 전문가 RSS) 하루 1회 자동수집 — 증분이라 새 글만 적재.
    best-effort(개별 실패 무시), fanding tt 만료 등은 조용히 스킵. 새 인사이트/시황 반영 위해 캐시 무효화.
    사이클 확정 국면 lead 섹터 종목 뉴스(kb.refresh)도 하루 1회 soft 포함."""
    if db.kv_get("kb_collect_date") == _kst_today():
        return
    got = False
    for fn in (kb.collect_fanding, kb.collect_outstanding, kb.collect_youtube, kb.collect_rss_macro):
        try:
            out = fn()
            got = got or bool(out.get("imported") or out.get("macro"))
        except Exception as e:
            log.warning("KB 자동수집 실패(%s): %s", getattr(fn, "__name__", "?"), type(e).__name__)
    try:  # 확정 국면 주도섹터 + BUY/보유/관심 — 종목 뉴스 다이제스트
        targets = _kb_targets()
        if targets:
            out = kb.refresh(targets)
            got = got or bool(out.get("updated"))
    except Exception as e:
        log.warning("KB 종목 자동수집 실패: %s", type(e).__name__)
    if got:
        _signals.cache_clear()
        _macro.cache_clear()
    try:  # US 재무 백필 — EDGAR(순이익·자기자본, 무료·무제한) 위주 + AV(섹터 등) 소량. 여러 날 걸쳐 전량 채움
        us = [u["ticker"] for u in store.load_us_universe()]
        if us:
            store.fetch_us_fundamentals_edgar(us, max_calls=60)  # EDGAR companyfacts → PER/PBR
            store.fetch_us_fundamentals(us, max_calls=20)        # AV → shares/sector 보조
            _clear_us_signal_caches()
    except Exception as e:
        log.warning("US 재무 백필 실패: %s", type(e).__name__)
    # 최근 이슈 흐름은 관리자 수동 refresh만(Sonnet 비용). 일일 자동 호출 없음.
    db.kv_set("kb_collect_date", _kst_today())


def _refresh_live_quotes(open_markets: list[str]) -> None:
    """열린 시장 종목의 토스 현재가를 배치 조회해 store에 실시간가 오버레이 설정 → 시그널·현재가
    캐시 무효화. 봇 run_once는 store.load_price_series()를 읽으므로 자동으로 실시간가 기준이 된다.
    열린 시장 없거나 토스 미가용 시 오버레이 해제(종가 복귀). best-effort(실패 무시)."""
    from signal_desk.ingest import toss
    if not open_markets:
        store.clear_live_quotes(); store.note_live_attempt("closed")
        _signals.cache_clear(); _clear_us_signal_caches(); _quotes.cache_clear(); _regime.cache_clear()
        return
    if not toss.available():
        store.clear_live_quotes(); store.note_live_attempt("toss_off", open_markets)
        _signals.cache_clear(); _clear_us_signal_caches(); _quotes.cache_clear(); _regime.cache_clear()
        return
    syms: list[str] = []
    if "kr" in open_markets:
        syms += [u["ticker"] for u in store.load_universe()]
    if "us" in open_markets:
        syms += [u["ticker"] for u in store.load_us_universe()]
    try:
        quotes = toss.prices(syms) if syms else {}
    except Exception as e:
        log.warning("실시간가 조회 실패(무시): %s", type(e).__name__)
        store.note_live_attempt("no_quotes", open_markets)
        return
    if quotes:
        store.set_live_quotes(quotes)
        store.note_live_attempt("ok", open_markets)
        _signals.cache_clear(); _clear_us_signal_caches(); _quotes.cache_clear(); _regime.cache_clear()
    else:  # 토큰 실패 등으로 빈 응답 — 오버레이 유지 안 함, 시도 기록만
        store.note_live_attempt("no_quotes", open_markets)


def _open_markets() -> list[str]:
    return [m for m, is_open in
            (("kr", bot.is_market_hours()), ("us", bot.is_us_market_hours())) if is_open]


def _quote_loop_iteration() -> None:
    """시세 전용 틱 — 토스 현재가 오버레이만(봇/LLM 없음)."""
    _refresh_live_quotes(_open_markets())


def _bot_loop_iteration() -> None:
    """봇·LLM·백필 루프 1회분(시세 갱신은 _quote_loop가 담당).
    동기 블로킹이라 asyncio.to_thread로 돌린다."""
    _daily_kb_collect()  # 외부 소스(미주은·오건영·유튜브) 하루 1회 자동수집(공용)
    enabled = db.user_bots_enabled()
    open_markets = _open_markets()
    try:  # 배포 환경 US 시세 자동 점진 적재(us_prices는 gitignore로 캐시 없음) — 다 차면 no-op
        bf = _backfill_us_prices_batch(25)
        if bf["filled"]:
            _clear_us_signal_caches()
            log.info("US 시세 자동 백필 %d종목(잔여 %s)", bf["filled"], bf["missing"])
    except Exception as e:
        log.warning("US 시세 자동 백필 실패(무시): %s", type(e).__name__)
    about_n = moves_n = 0
    try:  # 사업 개요(무엇을 하는 회사) LLM 증분 백필 — 캐시 없는 종목만, 다 차면 no-op
        about_n = _backfill_about_batch(15)
        if about_n:
            log.info("사업 개요 자동 백필 %d종목", about_n)
    except Exception as e:
        log.warning("사업 개요 자동 백필 실패(무시): %s", type(e).__name__)
    try:  # 최근 행보 LLM 증분 백필 — KB 문서 있고 캐시 오래된 종목만(새 뉴스 반영)
        moves_n = _backfill_moves_batch(10)
        if moves_n:
            log.info("최근 행보 자동 백필 %d종목", moves_n)
    except Exception as e:
        log.warning("최근 행보 자동 백필 실패(무시): %s", type(e).__name__)
    if about_n or moves_n:  # evaluate는 그대로, 리스트 문구만 갱신
        _us_signal_items.cache_clear()
    for uid in enabled:  # 장중인 시장만 체결(장외 스킵)
        for mkt in open_markets:
            try:  # 예약 주문 먼저(목표가+추격폭 이내만) — run_once와 별개 경로
                res = bot.execute_reservations(uid, market=mkt)
                if res.get("executed") and uid not in bot.REFERENCE_BOTS:
                    filled = [x for x in res["executed"] if x.get("status") == "filled"]
                    if filled:
                        log.info("예약 체결(uid=%s, %s): %d건", uid, mkt, len(filled))
            except Exception as e:
                log.warning("예약 실행 실패(uid=%s, %s): %s", uid, mkt, type(e).__name__)
            result = bot.run_once(uid, market=mkt)
            if not result.get("ok"):
                log.info("봇 실행 스킵(uid=%s, %s): %s", uid, mkt, result.get("reason"))
            elif uid not in bot.REFERENCE_BOTS:  # 실제 유저 체결만 푸시(레퍼런스 봇은 제외)
                _push_trades(mkt, result)
        if uid not in bot.REFERENCE_BOTS:
            _scan_alerts(uid)  # 관심종목 시그널 변동 능동 스캔 → 텔레그램 푸시(앱 안 열어도)
    # 하루 1회(평일 마감 후): 공용 KB 갱신 + 유저별 종가 스냅샷
    now = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    if enabled and now.weekday() < 5 and now.time() >= datetime.time(15, 40) \
            and db.kv_get("bot_daily_snap") != _kst_today():
        try:
            kb.refresh(_kb_targets())
            _signals.cache_clear()
        except Exception as e:
            log.warning("마감후 KB 갱신 실패: %s", e)
        try:   # 종목별·시장 수급(외국인·기관 순매수) 일일 갱신 — 수급 팩터/국면이 신선하게 유지되도록
            store.fetch_flows(store.load_universe())
            store.fetch_market_flow()
            _signals.cache_clear(); _regime.cache_clear()
        except Exception as e:
            log.warning("마감후 수급 갱신 실패: %s", type(e).__name__)
        try:   # 공매도 거래비중 일일 갱신 — 공매도 팩터 신선화(KRX, 마감후 확정)
            store.fetch_short(store.load_universe())
            _signals.cache_clear()
        except Exception as e:
            log.warning("마감후 공매도 갱신 실패: %s", type(e).__name__)
        try:   # 애널 컨센서스 일별 PIT 스냅샷 축적 — 리비전/목표가v2용(아직 미반영, 데이터만 쌓음)
            store.fetch_consensus(store.load_universe())
        except Exception as e:
            log.warning("마감후 컨센서스 수집 실패: %s", type(e).__name__)
        try:
            store.snapshot_signals(_signals())  # 팩터 PIT 스냅샷 누적(향후 팩터 백테스트용)
        except Exception as e:
            log.warning("시그널 스냅샷 실패: %s", type(e).__name__)
        try:
            climate.snapshot_shadow(_signals())  # 기후 vs 기존 kind 관측(봇 미연동)
        except Exception as e:
            log.warning("기후 shadow 스냅샷 실패: %s", type(e).__name__)
        for uid in enabled:
            bot.snapshot_positions(uid, "kr")
            bot.snapshot_positions(uid, "us")
        db.kv_set("bot_daily_snap", _kst_today())


async def _quote_loop():
    """장중 토스 현재가 전용 루프(기본 10분). 봇/LLM과 분리해 시세만 자주 갱신."""
    interval = config.quote_refresh_interval_minutes() * 60
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(_quote_loop_iteration)
        except Exception as e:
            log.error("시세 갱신 루프 오류: %s", e)
        await asyncio.sleep(interval)


async def _bot_loop():
    """봇·LLM 백그라운드 루프(기본 30분). 시그널은 공용, 계좌는 paper.

    실제 체결은 각 시장 장중에만 — KR 09:00~15:20, US KST 근사 22:30~06:00.
    시세 오버레이는 _quote_loop가 따로 돌린다. KB·종가 스냅샷은 하루 1회(kv 가드).
    본문은 동기 블로킹이라 to_thread로 돌려 헬스체크/API를 막지 않는다."""
    interval = config.bot_run_interval_minutes() * 60
    await asyncio.sleep(8)  # 시세 루프가 먼저 한 틱 돌 여유
    while True:
        try:
            await asyncio.to_thread(_bot_loop_iteration)
        except Exception as e:
            log.error("자동매매봇 루프 오류: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        bot.ensure_reference_bots()  # 공용 레퍼런스 봇(성향별) 부트스트랩 — 루프가 자동 운용
    except Exception as e:
        log.warning("레퍼런스 봇 부트스트랩 실패: %s", type(e).__name__)
    quote_task = asyncio.create_task(_quote_loop())
    bot_task = asyncio.create_task(_bot_loop())
    yield
    quote_task.cancel()
    bot_task.cancel()


app = FastAPI(title="signal-desk", lifespan=_lifespan)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """인증된 유저만 데이터 API 접근. /api/auth/* 와 비-API(/, 정적)는 허용."""
    p = request.url.path
    if p.startswith("/api/") and not p.startswith(_OPEN_PREFIXES):
        if not _uid(request):
            return JSONResponse({"error": "인증이 필요합니다.", "auth": False}, status_code=401)
        if p in _ADMIN_PATHS and not _require_admin(request):  # 관리자 전용(엔진·KB적재·갱신)
            return JSONResponse({"error": "관리자 권한이 필요합니다.", "admin": False}, status_code=403)
    return await call_next(request)


# 관리자만 접근 가능한 엔드포인트(정확 경로 매칭 — /api/kb/{ticker} 조회는 영향 없음)
_ADMIN_PATHS = {
    "/api/refresh", "/api/engine/config", "/api/engine/reset", "/api/engine/qualitative-promotion",
    "/api/backtest/analysis",
    "/api/kb/refresh", "/api/kb/import", "/api/kb/import-file", "/api/kb/documents", "/api/kb/digests",
    "/api/kb/events", "/api/kb/sources", "/api/kb/sources/lifecycle",
    "/api/kb/collect-fanding", "/api/kb/collect-outstanding", "/api/kb/collect-youtube", "/api/kb/collect-rss",
    "/api/shortform/generate", "/api/shortform/generate-performance",
    "/api/shortform/queue", "/api/shortform/candidates",
    "/api/brain/proposals", "/api/brain/proposals/refresh", "/api/engine/config/history",
    "/api/engine/llm-usage",
    "/api/data-health", "/api/egress-ip",
    "/api/hypothesis/refresh",
    "/api/external-watch", "/api/external-watch/clear", "/api/external-watch/refresh-kb",
}


# ---------- 인증 ----------
def _set_auth_cookie(r: JSONResponse, token: str) -> None:
    # prod(HTTPS)에서는 secure 플래그로 평문 전송 차단. httponly로 JS 접근 차단(XSS 완화).
    r.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                 secure=config.is_prod(), max_age=60 * 60 * 24 * 30)


# 간단한 인메모리 레이트리밋(브루트포스 완화) — IP+동작별 슬라이딩 윈도우
_rl_hits: dict[str, list[float]] = {}


def _rate_limited(request: Request, action: str, limit: int = 8, window: int = 300) -> bool:
    ip = (request.client.host if request.client else "?") + ":" + action
    now = time.time()
    hits = [t for t in _rl_hits.get(ip, []) if now - t < window]
    hits.append(now)
    _rl_hits[ip] = hits
    return len(hits) > limit


@app.post("/api/auth/signup")
def auth_signup(request: Request, data: dict = Body(...)):
    if _rate_limited(request, "signup", limit=5):
        return JSONResponse({"ok": False, "error": "요청이 너무 잦습니다. 잠시 후 다시 시도하세요."}, status_code=429)
    token, err = auth.signup(data.get("email", ""), data.get("pw", ""))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    r = JSONResponse({"ok": True})
    _set_auth_cookie(r, token)
    return r


@app.post("/api/auth/login")
def auth_login(request: Request, data: dict = Body(...)):
    if _rate_limited(request, "login", limit=8):
        return JSONResponse({"ok": False, "error": "로그인 시도가 너무 잦습니다. 잠시 후 다시 시도하세요."}, status_code=429)
    token, err = auth.login(data.get("email", ""), data.get("pw", ""))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
    r = JSONResponse({"ok": True})
    _set_auth_cookie(r, token)
    return r


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    if not u:
        return JSONResponse({"auth": False}, status_code=401)
    profile = db.profile_get(u["id"])
    return {"auth": True, "email": u["email"], "profile": profile, "onboarded": bool(profile),
            "is_admin": config.is_admin(u["email"])}


def _require_admin(request: Request):
    """관리자 전용 엔드포인트 가드 — 화이트리스트 밖이면 403."""
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    return config.is_admin(u["email"]) if u else False


# ---------- 프로필(온보딩) ----------
@app.get("/api/profile")
def profile_get(request: Request):
    return db.profile_get(_uid(request))


@app.put("/api/profile")
def profile_put(request: Request, data: dict = Body(...)):
    db.profile_set(_uid(request), data)
    return {"ok": True}


# ---------- 워치리스트(즐겨찾기, kind='ticker') ----------
@app.get("/api/favorites")
def favorites_get(request: Request):
    return {"favorites": db.fav_list(_uid(request))}


@app.post("/api/favorites")
def favorites_add(request: Request, data: dict = Body(...)):
    db.fav_add(_uid(request), data.get("kind", "ticker"), data.get("key", ""), data.get("label", ""))
    return {"ok": True}


@app.delete("/api/favorites")
def favorites_del(request: Request, kind: str, key: str):
    db.fav_remove(_uid(request), kind, key)
    return {"ok": True}


# ---------- 알림 (#16 관심종목 시그널 변동) ----------
_KIND_KO = {"BUY": "매수", "SELL": "매도", "HOLD": "관망"}


def _scan_alerts(uid: int) -> None:
    """관심종목의 현재 시그널 kind를 직전 관측치와 비교해 변동 시 알림 생성(최초 관측은 기록만).
    조회 시점에 계산 — 유저가 앱을 열 때 '마지막 확인 이후 바뀐 것'을 잡는다."""
    favs = [f["key"] for f in db.fav_list(uid) if f["kind"] == "ticker"]
    if not favs:
        return
    sigmap = {s.ticker: s for s in _signals()} if store.is_ready() else {}
    sigmap.update(_us_signals())
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
    prev = db.alert_state_all(uid)
    for t in favs:
        sig = sigmap.get(t)
        if not sig:
            continue
        cur, old = sig.kind, prev.get(t)
        if old is None:
            db.alert_state_set(uid, t, cur)  # 최초 관측은 기록만(알림 없음)
        elif old != cur:
            name = names.get(t, t)
            msg = f"시그널 {_KIND_KO.get(old, old)} → {_KIND_KO.get(cur, cur)} (점수 {sig.score:+.2f})"
            db.alert_add(uid, t, name, msg)
            db.alert_state_set(uid, t, cur)
            notify.push(f"📊 {name}({t}) {msg}")  # 텔레그램 능동 푸시(미설정 시 no-op, alert_state로 중복 방지)


def _push_trades(market: str, result: dict) -> None:
    """봇 체결(매수·매도)을 텔레그램으로 푸시. note(청산 사유 등)를 사람이 읽기 쉽게 표기."""
    if not notify.available():
        return
    lines = []
    for b in result.get("buys", []):
        lines.append(f"🟢 매수 {b.get('name', b.get('ticker'))} {b.get('qty')}주")
    for s in result.get("sells", []):
        detail = s.get("note") or s.get("reason") or ""
        lines.append(f"🔴 매도 {s.get('name', s.get('ticker'))} {s.get('qty')}주"
                     + (f" · {detail}" if detail else ""))
    if lines:
        notify.push(f"🤖 봇 체결 ({market.upper()})\n" + "\n".join(lines[:10]))


@app.get("/api/alerts")
def alerts_get(request: Request):
    """관심종목 시그널 변동 알림 목록 + 안읽음 수. 조회 시 변동을 스캔해 새 알림을 만든다."""
    uid = _uid(request)
    _scan_alerts(uid)
    return {"alerts": db.alerts_list(uid, 30), "unread": db.alerts_unread(uid)}


@app.post("/api/alerts/read")
def alerts_read(request: Request):
    db.alerts_mark_read(_uid(request))
    return {"ok": True}


# ---------- 실보유 종목 + 리밸런싱 ----------
@lru_cache(maxsize=1)
def _all_tickers():
    """보유종목 검색용 국내+해외 통합 목록 [{ticker, name, market}]."""
    out = [{"ticker": u["ticker"], "name": u["name"], "market": "국내"} for u in store.load_universe()]
    out += [{"ticker": u["ticker"], "name": us_ko.name_ko(u["ticker"], u["name"]), "market": "해외"}
            for u in store.load_us_universe()]
    return out


@app.get("/api/tickers")
def tickers_get():
    """보유종목 검색 자동완성용 통합 티커 목록(국내 KOSPI + 해외 S&P500)."""
    return {"tickers": _all_tickers()}


@app.get("/api/holdings")
def holdings_get(request: Request):
    return {"holdings": db.holdings_list(_uid(request))}


@app.post("/api/holdings")
def holdings_set(request: Request, data: dict = Body(...)):
    ticker = str(data.get("ticker", "")).strip()
    if not ticker:
        return JSONResponse({"ok": False, "error": "종목코드 필요"}, status_code=400)
    db.holdings_set(_uid(request), ticker, float(data.get("qty", 0)), float(data.get("avg_price", 0)))
    return {"ok": True}


@app.delete("/api/holdings")
def holdings_del(request: Request, ticker: str):
    db.holdings_remove(_uid(request), ticker)
    return {"ok": True}


@app.get("/api/holdings/dividends")
def holdings_dividends_get(request: Request):
    """내 보유종목 중 배당주의 예상 배당(내 포트폴리오 탭). 보유수량×주당배당=연배당, ÷12=월평균.
    KR(₩)·US($)는 통화가 달라 합치지 않고 통화별로 집계한다. 지급 빈도(div_months)도 함께 내려준다."""
    hs = db.holdings_list(_uid(request))
    if not hs:
        return {"ready": False, "items": [], "totals": {}}
    kr, us = store.kr_dividends(), store.us_dividends()
    kr_names = {u["ticker"]: u["name"] for u in store.load_universe()}
    us_names = {u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()}
    items, totals = [], {}
    for h in hs:
        t, qty = h["ticker"], h.get("qty") or 0
        if t in us:
            d, cur, name = us[t], "USD", us_names.get(t, t)
        elif t in kr:
            d, cur, name = kr[t], "KRW", kr_names.get(t, t)
        else:
            continue  # 배당 없는(또는 미수집) 보유는 제외
        annual = (d.get("dps") or 0) * qty
        if annual <= 0:
            continue
        items.append({"ticker": t, "name": name, "qty": qty, "currency": cur, "dps": d["dps"],
                      "div_yield": d.get("div_yield"), "div_months": d.get("div_months") or [],
                      "annual": round(annual, 2), "monthly": round(annual / 12, 2)})
        tv = totals.setdefault(cur, {"annual": 0.0, "monthly": 0.0, "count": 0})
        tv["annual"] += annual
        tv["monthly"] += annual / 12
        tv["count"] += 1
    items.sort(key=lambda x: x["annual"], reverse=True)
    for tv in totals.values():
        tv["annual"], tv["monthly"] = round(tv["annual"], 2), round(tv["monthly"], 2)
    return {"ready": bool(items), "items": items, "totals": totals}


def _holdings_by_market(holdings: list[dict], market: str | None) -> list[dict]:
    """보유종목을 시장별로 분리 — KR=6자리 숫자 코드, US=영문 티커. market None이면 전체."""
    if market == "kr":
        return [h for h in holdings if str(h.get("ticker", "")).isdigit()]
    if market == "us":
        return [h for h in holdings if not str(h.get("ticker", "")).isdigit()]
    return holdings


@app.post("/api/rebalance")
def rebalance_post(request: Request, data: dict = Body(default={})):
    """내 보유종목을 시그널·성향 목표배분에 맞춰 리밸런싱 제안 + LLM 해설. market=kr|us로 시장 분리(기본 전체)."""
    holdings = _holdings_by_market(db.holdings_list(_uid(request)), data.get("market"))
    if not holdings:
        return {"ready": False, "reason": "해당 시장의 보유종목이 없습니다."}
    if not store.is_ready():
        return {"ready": False, "reason": "시세 데이터가 없습니다 — /api/refresh 먼저."}
    # 국내+해외 시그널·시세·종목명 병합(혼합 포트폴리오 지원)
    prices = {**store.load_price_series(), **store.load_us_price_series()}
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
    sigmap = {s.ticker: s for s in _signals()}
    sigmap.update(_us_signals())
    style = strategy.normalize(data.get("style") or "balanced")
    plan = rebalance.propose(holdings, sigmap, prices, names, strategy.bot_params(style))
    context = {"regime": _regime().get("regime"), "macro_bias": _macro().get("bias")}
    plan["summary"] = rebalance.explain(plan, strategy.STYLE_LABEL.get(style, style), context)
    plan["ready"] = True
    plan["style_label"] = strategy.STYLE_LABEL.get(style, style)
    return plan


@app.post("/api/scenario")
def scenario_post(request: Request, data: dict = Body(default={})):
    """내 보유종목을 부트스트랩 몬테카를로로 전략별 N년 후 가치 분포로 투영(#9). market=kr|us로 시장 분리."""
    holdings = _holdings_by_market(db.holdings_list(_uid(request)), data.get("market"))
    if not holdings:
        return {"ready": False, "reason": "해당 시장의 보유종목이 없습니다."}
    if not store.is_ready():
        return {"ready": False, "reason": "시세 데이터가 없습니다 — /api/refresh 먼저."}
    prices = {**store.load_price_series(), **store.load_us_price_series()}
    years = min(max(int(data.get("years", 3)), 1), 10)
    return scenario.project(holdings, prices, years=years)


@app.get("/api/portfolio/heatmap")
def portfolio_heatmap(request: Request, market: str = ""):
    """내 보유종목을 섹터별로 묶은 히트맵(#12) — 평가액 크기 + 손익률 색상. market=kr|us로 시장 분리."""
    holdings = _holdings_by_market(db.holdings_list(_uid(request)), market or None)
    if not holdings:
        return {"ready": False, "reason": "해당 시장의 보유종목이 없습니다."}
    prices = {**store.load_price_series(), **store.load_us_price_series()}
    us_sec = {u["ticker"]: u.get("sector") for u in store.load_us_universe()}
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
    items = []
    for h in holdings:
        closes = prices.get(h["ticker"])
        if not closes:
            continue
        px = float(closes[-1])
        val = px * float(h.get("qty") or 0)
        if val <= 0:
            continue
        sector = sectors.sector_of(h["ticker"]) or us_ko.sector_ko(us_sec.get(h["ticker"])) or "기타"
        avg = float(h.get("avg_price") or 0)
        pnl_pct = round((px / avg - 1) * 100, 2) if avg else 0.0
        items.append({"ticker": h["ticker"], "name": names.get(h["ticker"], h["ticker"]),
                      "sector": sector, "value": round(val, 2), "pnl_pct": pnl_pct})
    if not items:
        return {"ready": False, "reason": "시세가 있는 보유종목이 없습니다."}
    return {"ready": True, "items": items}


# ---------- 시그널 (실데이터, store 캐시 기반) ----------
@lru_cache(maxsize=1)
def _signals():
    cfg, _ = signalcfg.effective_config(_regime(), _macro(), flow_result=store.load_market_flow())  # 약세·비우호·외인기관 순매도면 매수 기준 상향
    return evaluate(store.load_universe(), store.load_price_series(), store.load_fundamentals(),
                    config=cfg, sentiment=kb.sentiment_map(), flows=store.load_flows(),
                    shorts=store.load_short())


@lru_cache(maxsize=1)
def _backtest():
    return backtest_summary(store.load_price_series(), config=signalcfg.get_config())


@lru_cache(maxsize=1)
def _backtest_analysis():
    """point-in-time 재무 반영 요약 + 팩터별 기여도 + 워크포워드 — 관리자 정밀 분석용."""
    cfg = signalcfg.get_config()
    prices = store.load_price_series()
    dates = store.load_dates_by_ticker()
    hist = store.load_fundamentals_history()
    return {
        "pit": backtest_summary(prices, cfg, dates, hist),
        "factors": factor_contribution(prices, cfg, dates, hist),
        "walkforward": walk_forward(prices, cfg, dates, hist),
        "has_pit": bool(hist),
    }


@lru_cache(maxsize=1)
def _valuation():
    return valuation.screen(store.load_universe(), store.load_fundamentals())


@lru_cache(maxsize=1)
def _quotes():
    return store.load_quotes()


@lru_cache(maxsize=1)
def _regime():
    return regime.classify(store.load_price_series())


@lru_cache(maxsize=1)
def _macro():
    indicators = store.load_macro()          # 미국(FRED)
    kr = store.load_macro_kr()               # 한국(ECOS) — favor·reason 사전판정 포함
    # 정량 지표(FRED+ECOS) + 정성 내러티브(미주은 시황 코멘터리 — 개별 종목엔 미반영)
    return {"indicators": indicators, "narrative": kb.macro_digest(),
            **macro.read(indicators, extra=kr)}


def _clear_us_signal_caches() -> None:
    """US evaluate + 리스트 조립 캐시 동시 무효화 — 한 쪽만 비우면 점수/시세가 어긋난다."""
    _us_signals.cache_clear()
    _us_signal_items.cache_clear()


def _decision_payload(r) -> dict:
    dec = getattr(r, "decision", None)
    if dec is not None and hasattr(dec, "to_dict"):
        return dec.to_dict()
    return {
        "buy_blocked": bool(getattr(r, "event_risk", False)),
        "holding_action": "exit" if getattr(r, "event_severity", "") == "critical"
        else ("trim" if getattr(r, "event_severity", "") == "serious" else "none"),
        "event_id": None,
        "severity": getattr(r, "event_severity", None) or None,
        "summary": getattr(r, "event_note", "") or "",
        "policy_version": "p2",
    }


def _attention_events(ticker: str, limit: int = 5) -> list[dict]:
    """시그널 상세 Attention — candidate 카드(조사 필요). Decision/점수 미반영."""
    items = db.kb_events_list(limit=limit, ticker=ticker, status="candidate")
    out = []
    for it in items:
        out.append({
            "id": it["id"], "severity": it.get("severity"), "summary": it.get("summary"),
            "status": it.get("status"), "event_type": it.get("event_type"),
            "direction": it.get("direction"), "trust_tier": it.get("trust_tier"),
            "confidence": it.get("confidence"),
            "evidence": db.kb_event_evidence(it["id"]),
        })
    return out


def _list_row_from_signal(r, *, name: str, sector: str | None, price, change_pct,
                          mktcap, vol, vol_avg, per, pbr, roe=None, div_yield=None) -> dict:
    """리스트 API용 요약 행 — reasons/narrative/about/moves/target/kb 제외(클릭 시 detail)."""
    dec = _decision_payload(r)
    return {
        "ticker": r.ticker, "name": name, "score": round(r.score, 4), "kind": r.kind,
        "confidence": r.confidence, "factor_scores": getattr(r, "factor_scores", {}) or {},
        "event_risk": bool(dec.get("buy_blocked")),
        "decision_buy_blocked": bool(dec.get("buy_blocked")),
        "earnings_soon": bool(getattr(r, "earnings_soon", False)),
        "earnings_date": getattr(r, "earnings_date", None),
        "valuation_percentile": getattr(r, "valuation_percentile", None),
        "price": price, "change_pct": change_pct, "mktcap": mktcap,
        "vol": vol, "vol_avg": vol_avg, "per": per, "pbr": pbr, "roe": roe,
        "div_yield": div_yield, "sector": sector,
        "opp_tags": opportunity.classify(r),
    }


@lru_cache(maxsize=1)
def _us_signal_items() -> list[dict]:
    """미국(S&P500) 시그널 **리스트 요약** — 스크리너·정렬에 필요한 필드만.
    about/moves/target/reasons/narrative는 `/detail`에서 클릭 시 로드(페이로드·OOM 완화)."""
    sig = _us_signals()
    if not sig:
        return []
    sector_of = {u["ticker"]: u.get("sector") for u in store.load_us_universe()}
    hist, quotes = store.load_us_price_bundle()  # parquet 1회(시리즈+거래량)
    mcaps = store.us_marketcaps(hist)
    items = []
    for r in sig.values():
        closes = hist.get(r.ticker) or []
        price = closes[-1] if closes else None
        prev = closes[-2] if len(closes) >= 2 else None
        q = quotes.get(r.ticker) or {}
        mc = mcaps.get(r.ticker) or {}
        sector = us_ko.sector_ko(sector_of.get(r.ticker))
        items.append(_list_row_from_signal(
            r, name=us_ko.name_ko(r.ticker, r.name), sector=sector,
            price=price,
            change_pct=round((price / prev - 1) * 100, 2) if (price and prev) else None,
            mktcap=mc.get("mktcap"), vol=q.get("vol"), vol_avg=q.get("vol_avg"),
            per=mc.get("per"), pbr=mc.get("pbr")))
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


_ACTIVE_SIGNAL_KINDS = frozenset({"STRONG_BUY", "BUY", "SELL", "STRONG_SELL"})


def _us_signal_detail(ticker: str) -> dict | None:
    """US 종목 상세(클릭 시) — 리스트에 없던 about/moves/target/reasons/narrative."""
    r = _us_signals().get(ticker)
    if not r:
        return None
    sector_of = {u["ticker"]: u.get("sector") for u in store.load_us_universe()}
    hist, quotes = store.load_us_price_bundle()
    mcaps = store.us_marketcaps(hist)
    us_pers = sorted(mc["per"] for mc in mcaps.values() if mc.get("per") and mc["per"] > 0)
    us_med_per = us_pers[len(us_pers) // 2] if us_pers else None
    closes = hist.get(ticker) or []
    price = closes[-1] if closes else None
    prev = closes[-2] if len(closes) >= 2 else None
    q = quotes.get(ticker) or {}
    mc = mcaps.get(ticker) or {}
    sector = us_ko.sector_ko(sector_of.get(ticker))
    name = us_ko.name_ko(ticker, r.name)
    d = asdict(r)
    d["name"] = name
    d["price"] = price
    d["change_pct"] = round((price / prev - 1) * 100, 2) if (price and prev) else None
    d["vol"] = q.get("vol"); d["vol_avg"] = q.get("vol_avg")
    d["mktcap"] = mc.get("mktcap"); d["per"] = mc.get("per"); d["pbr"] = mc.get("pbr")
    d["sector"] = sector
    d["intro"] = f"{sector} 섹터" if sector else None
    d["intro_desc"] = None
    # 상세 클릭 시 개요 캐시 없으면 온디맨드 생성(캐시됨) — 처음 보는 종목 이해도
    from signal_desk import llm as llm_mod
    d["about"] = company.about(
        ticker, name, sector, "us",
        generate=True, model=llm_mod.ABOUT_QUALITY_MODEL,
    )
    d["moves"] = company.recent_moves(ticker, name)
    d["kb"] = None
    d["target"] = target.compute(price, mc.get("per"), us_med_per, closes)
    d["opp_tags"] = opportunity.classify(r)
    d["decision"] = _decision_payload(r)
    d["attention_events"] = _attention_events(ticker)
    if d["decision"].get("buy_blocked") and r.kind in ("BUY", "STRONG_BUY"):
        d["attention_conflict"] = True  # 매수 신호 vs 이벤트 리스크
    climate.annotate_rows([d])
    return d


def _kr_signal_detail(ticker: str) -> dict | None:
    """KR 종목 상세(클릭 시)."""
    r = next((s for s in _signals() if s.ticker == ticker), None)
    if not r:
        return None
    q = (_quotes().get(ticker) or {})
    f = (store.load_fundamentals().get(ticker) or {})
    fundamentals = store.load_fundamentals()
    med_per = target.median_per(fundamentals)
    sector = sectors.sector_of(ticker)
    sec_med = target.sector_median_per(fundamentals, {t: sectors.sector_of(t) for t in fundamentals})
    c = (store.load_consensus_latest().get(ticker) or {})
    pos = valuechain.company_position(ticker)
    d = asdict(r)
    d["price"] = q.get("price"); d["change_pct"] = q.get("change_pct")
    d["mktcap"] = q.get("mktcap"); d["vol"] = q.get("vol"); d["vol_avg"] = q.get("vol_avg")
    d["per"] = f.get("per"); d["pbr"] = f.get("pbr"); d["roe"] = f.get("roe")
    d["debt_ratio"] = f.get("debt_ratio"); d["revenue_growth"] = f.get("revenue_growth")
    dps, px = f.get("dps"), d.get("price")
    d["div_yield"] = round(dps / px * 100, 2) if (dps and px) else None
    d["sector"] = sector
    d["intro"] = f"{pos['sector']} 밸류체인 · {pos['stage']}" if pos else None
    d["intro_desc"] = pos["stage_desc"] if pos else None
    from signal_desk import llm as llm_mod
    d["about"] = company.about(
        ticker, r.name, sector, "kr",
        generate=True, model=llm_mod.ABOUT_QUALITY_MODEL,
    )
    d["moves"] = company.recent_moves(ticker, r.name)
    dg = db.kb_digest_get(ticker)
    d["kb"] = {"sentiment": dg["sentiment"], "summary": dg["summary"], "points": dg["points"]} if dg else None
    d["opp_tags"] = opportunity.classify(r)
    d["target"] = target.compute(d["price"], f.get("per"), sec_med.get(sector) or med_per,
                                 store.load_price_series().get(ticker),
                                 analyst_target=c.get("price_target_mean"), fwd_eps=c.get("fwd1_eps"))
    d["decision"] = _decision_payload(r)
    d["attention_events"] = _attention_events(ticker)
    if d["decision"].get("buy_blocked") and r.kind in ("BUY", "STRONG_BUY"):
        d["attention_conflict"] = True
    climate.annotate_rows([d])
    return d


def _annotate_external_watch(items: list[dict]) -> list[dict]:
    """조사 큐 소속 여부 — 점수 가산 없음, UI 뱃지/필터용."""
    try:
        from signal_desk import external_watch
        watch = external_watch.ticker_set()
    except Exception:
        watch = set()
    if not watch:
        for it in items:
            it["external_watch"] = False
        return items
    for it in items:
        it["external_watch"] = it.get("ticker") in watch
    return items


@app.get("/api/signals")
def signals_get(market: str = "kospi"):
    """시그널 리스트(요약). 상세 필드(about/moves/target/reasons/narrative/kb)는
    GET /api/signals/{ticker}/detail 로 클릭 시 로드."""
    if market == "us":
        items = _us_signal_items()
        if not items:
            return {"ready": False, "items": [], "message": "미국 종목 시세가 아직 없습니다 — 백필 후 표시됩니다."}
        return {"ready": True, "items": climate.annotate_rows(_annotate_external_watch(items)), "slim": True}
    if not store.is_ready():
        return {"ready": False, "items": [], "message": "아직 수집된 데이터가 없습니다. /api/refresh를 먼저 호출하세요."}
    items = []
    quotes = _quotes()
    fundamentals = store.load_fundamentals()
    for r in _signals():
        q = quotes.get(r.ticker) or {}
        f = fundamentals.get(r.ticker) or {}
        px = q.get("price")
        dps = f.get("dps")
        items.append(_list_row_from_signal(
            r, name=r.name, sector=sectors.sector_of(r.ticker),
            price=px, change_pct=q.get("change_pct"), mktcap=q.get("mktcap"),
            vol=q.get("vol"), vol_avg=q.get("vol_avg"),
            per=f.get("per"), pbr=f.get("pbr"), roe=f.get("roe"),
            div_yield=round(dps / px * 100, 2) if (dps and px) else None))
    return {"ready": True, "items": climate.annotate_rows(_annotate_external_watch(items)), "slim": True}


@app.get("/api/signals/{ticker}/detail")
def signal_detail_get(ticker: str, market: str = "kospi"):
    """종목 상세 — 리스트에 없는 해설·사업개요·목표가·KB. 차트와 병렬 fetch용."""
    item = _us_signal_detail(ticker) if market == "us" else _kr_signal_detail(ticker)
    if not item:
        return {"ready": False, "item": None}
    return {"ready": True, "item": item}


def _buylist(uid: int) -> list[dict]:
    """관심종목별 '매수까지 무엇이 남았는지' — 조정장 대기 데스크용. 현재 점수·유효 매수문턱·막는
    게이트를 투명하게. 예측이 아니라 '무엇을 기다리는지'를 보여준다(evidence-only, 매수 강권 X)."""
    favs = [f["key"] for f in db.fav_list(uid) if f["kind"] == "ticker"]
    if not favs:
        return []
    kr_sigs = {s.ticker: s for s in _signals()} if store.is_ready() else {}
    us_sigs = _us_signals()
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
    _, adapt = signalcfg.effective_config(_regime(), _macro(), flow_result=store.load_market_flow())
    kr_thr = adapt["effective_buy_threshold"]        # 국면 상향된 유효 매수문턱(KR)
    base_thr = signalcfg.get_config().buy_threshold  # US 등 기본
    out = []
    for t in favs:
        sig = kr_sigs.get(t) or us_sigs.get(t)
        if not sig:
            continue
        is_us = t not in kr_sigs
        thr = base_thr if is_us else kr_thr
        blockers = []
        if sig.event_risk or (getattr(sig, "decision", None) and sig.decision.buy_blocked):
            blockers.append({"key": "event", "label": "악재 이벤트(매수 차단)", "hint": "이벤트 해소까지 관망"})
        if sig.earnings_soon:
            edate = f"({sig.earnings_date})" if sig.earnings_date else ""
            blockers.append({"key": "earnings", "label": f"실적발표 임박{edate}", "hint": "발표 후 재평가"})
        if any("하락추세 확인" in r for r in sig.reasons):
            blockers.append({"key": "trend", "label": "하락추세", "hint": "종가가 20일선 회복 시 재평가"})
        gap = round(thr - sig.score, 2)
        if sig.kind in ("BUY", "STRONG_BUY") and not blockers:
            status, hint = "ready", "이미 매수 신호 — 확인해보세요"
        elif blockers:
            status, hint = "blocked", blockers[0]["hint"]
        else:
            status = "near" if gap <= 0.5 else "far"
            hint = f"점수 {sig.score:+.2f} · 매수문턱 {thr:.2f} — {max(gap, 0):.2f} 더 오르면 매수권"
        out.append({"ticker": t, "name": names.get(t, sig.name), "kind": sig.kind,
                    "score": round(sig.score, 2), "threshold": round(thr, 2), "gap": gap,
                    "blockers": blockers, "status": status, "hint": hint,
                    "market": "us" if is_us else "kr"})
    out.sort(key=lambda x: (len(x["blockers"]), x["gap"]))  # 매수에 가까운 순(게이트 적고 갭 작은)
    return out


@app.get("/api/regime-zone")
def regime_zone_get():
    """시장 국면 체온계 — 조정심화→바닥다지기→회복초기 ZONE 감지(예측 아님). 전 시장 대상."""
    if not store.is_ready():
        return {"ready": False}
    idx = [d["close"] for d in store.load_index_history()]
    return regime_zone.assess(store.load_price_series(), index_closes=idx, macro_result=_macro())


@app.get("/api/relative-strength")
def relative_strength_get():
    """상대강도 리더보드 — 시장(동일가중 지수) 대비 선방 종목 감시 렌즈(매수 신호 아님)."""
    if not store.is_ready():
        return {"ready": False, "items": []}
    idx = [d["close"] for d in store.load_index_history()]
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    return {"ready": True, "items": relative.leaderboard(store.load_price_series(), idx, names)}


@app.get("/api/buylist")
def buylist_get(request: Request):
    """조정장 매수 대기 리스트 — 관심종목별 매수까지 남은 조건. 로그인 필요."""
    uid = _uid(request)
    if not uid:
        return {"items": []}
    return {"items": _buylist(uid)}


_narr_locks: dict[str, threading.Lock] = {}
_narr_locks_mu = threading.Lock()


def _narr_lock(ticker: str) -> threading.Lock:
    with _narr_locks_mu:
        lk = _narr_locks.get(ticker)
        if lk is None:
            lk = threading.Lock()
            _narr_locks[ticker] = lk
        return lk


@app.get("/api/narrative")
def narrative_get(ticker: str):
    """시그널 해설 v2(#17) — BUY/SELL만 고품질 LLM(캐시). HOLD는 규칙 문장. 실패 시 v1 폴백."""
    from signal_desk import llm as llm_mod
    sig = next((s for s in _signals() if s.ticker == ticker), None) if store.is_ready() else None
    is_us = False
    if sig is None:
        sig = _us_signals().get(ticker)
        is_us = sig is not None
    if sig is None:
        return {"ok": False, "reason": "해당 종목 시그널이 없습니다."}
    # HOLD는 LLM 비용·노이즈 절감 — 규칙 해설만
    if sig.kind not in _ACTIVE_SIGNAL_KINDS:
        return {"ok": True, "narrative": sig.narrative, "source": "rule", "cached": False}
    with _narr_lock(ticker):
        names = {u["ticker"]: u["name"] for u in store.load_universe()}
        names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
        name = names.get(ticker, sig.name)
        if is_us:
            u = next((x for x in store.load_us_universe() if x["ticker"] == ticker), None) or {}
            sector = us_ko.sector_ko(u.get("sector"))
            market = "us"
        else:
            sector = sectors.sector_of(ticker)
            market = "kr"
        about_txt = company.about(
            ticker, name, sector, market,
            generate=True, model=llm_mod.ABOUT_QUALITY_MODEL,
        ) or ""
        dg = db.kb_digest_get(ticker)
        kb_summary = (dg or {}).get("summary") or ""
        # 데이터 스냅샷 해시로 캐시 키 — 시그널/KB/개요가 바뀌면 자동 무효화
        h = hashlib.md5(
            f"{sig.kind}|{round(sig.score, 1)}|{kb_summary}|{about_txt}".encode()
        ).hexdigest()[:12]
        key = f"narrv5:{ticker}:{h}"  # v5=opus 해설+회사개요 프롬프트
        cached = db.kv_get(key)
        if cached:
            return {"ok": True, "narrative": cached, "source": "llm", "cached": True}
        text = narrative.explain_llm(
            name, ticker, sig.kind, sig.score, sig.reasons, kb_summary,
            about=about_txt, model=llm_mod.SIGNAL_EXPLAIN_MODEL,
        )
        if text:
            db.kv_set(key, text)
            return {"ok": True, "narrative": text, "source": "llm", "cached": False}
        return {"ok": True, "narrative": sig.narrative, "source": "rule", "cached": False}


@app.get("/api/signal-scorecard")
def signal_scorecard_get():
    """실현 시그널 성적표(③ track record) — 봇의 실제 매수 판단이 3일 뒤 얼마나 맞았나.
    집계 + 최근 실현 판단 목록. 백테스트(가상 재현)와 달리 '실제 결정'의 사후검증."""
    resolved = [d for d in db.bot_decisions_recent(80)
                if d.get("action") == "buy" and d.get("outcome_pct") is not None]
    return {**db.bot_decision_scorecard(),
            "recent": [{"ticker": d["ticker"], "name": d["name"], "score": d["score"],
                        "outcome_pct": d["outcome_pct"], "ts": d["ts"]} for d in resolved[:20]]}


@app.get("/api/backtest")
def backtest_get():
    """시그널 적중률 성적표 — 가격기반(기술+낙폭과대). 정밀 분석은 /api/backtest/analysis."""
    if not store.is_ready():
        return {"ready": False}
    return {"ready": True, **_backtest()}


@app.get("/api/backtest/analysis")
def backtest_analysis_get():
    """정밀 분석 — point-in-time 재무 반영 성적표 + 팩터별 기여도 + 워크포워드 안정성."""
    if not store.is_ready():
        return {"ready": False}
    return {"ready": True, **_backtest_analysis()}


@app.get("/api/accuracy")
def accuracy_get():
    """실측 성과(track record) — 매일 저장된 실제 시그널(전 팩터·전 게이트)을 이후 실현 수익률과
    조인해 티어별 적중률·매수 정밀도·팩터 IC를 낸다. 백테스트(시뮬레이션)와 달리 '진짜 낸 신호'의
    성적이라 신뢰구축용. 스냅샷 도입일부터 누적되므로 초기 표본은 작다."""
    df = store.load_signal_history()
    if df.empty:
        return {"ready": False, "reason": "아직 저장된 시그널 이력이 없습니다(매일 마감 후 누적)."}
    rows = df.to_dict("records")
    return {"ready": True, **accuracy.realized_accuracy(rows, store.load_all_dated_closes())}


def _qualitative_promotion_payload() -> dict:
    df = store.load_signal_history()
    closes = store.load_all_dated_closes()
    metrics = accuracy.qualitative_promotion_metrics(
        [] if df.empty else df.to_dict("records"), closes)
    return signalcfg.qualitative_promotion_status(metrics)


@app.get("/api/engine/llm-usage")
def llm_usage_get(request: Request, days: int = 30):
    """이 앱 LLM 호출 추정 비용(공유 키와 분리). Anthropic 콘솔 ≠ 이 숫자."""
    _admin_or_403(request)
    return {"ready": True, **db.llm_usage_summary(days=max(1, min(int(days or 30), 365)))}


@app.get("/api/engine/qualitative-promotion")
def qualitative_promotion_get(request: Request):
    """P3 정성 shadow 관측 — 모드·실측 게이트. combine/봇 미반영."""
    _admin_or_403(request)
    return {"ready": True, **_qualitative_promotion_payload()}


@app.post("/api/engine/qualitative-promotion")
def qualitative_promotion_set(request: Request, data: dict = Body(...)):
    """관리자 승인으로 off↔shadow. priority/threshold는 거절."""
    _admin_or_403(request)
    mode = str((data or {}).get("mode") or "").strip().lower()
    note = str((data or {}).get("note") or "")
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    approved_by = (u or {}).get("email") or ""
    payload = _qualitative_promotion_payload()
    try:
        signalcfg.set_qualitative_mode(
            mode, approved_by=approved_by, note=note,
            gates_snapshot=payload.get("metrics", {}).get("gates"),
        )
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **_qualitative_promotion_payload()}


def _anchor_today_score(scores: list, ticker: str, market: str) -> list:
    """차트 점수 시계열의 '오늘'(마지막 점)을 현재 시그널 점수(전 팩터)로 맞춘다.
    과거 점은 시점별 재무·수급 스냅샷이 없어 가격기반(기술·낙폭·모멘텀) 재현이라 리스트 점수와
    다를 수 있는데, 최신 점만이라도 시그널 리스트와 일치시켜 혼동을 줄인다."""
    if not scores:
        return scores
    try:
        if market == "us":
            s = _us_signals().get(ticker)
            cur = round(s.score, 4) if s else None
        else:
            cur = next((round(s.score, 4) for s in _signals() if s.ticker == ticker), None)
    except Exception:
        cur = None
    if cur is not None:
        scores[-1] = cur
    return scores


# 프론트 차트 표시 상한(~1년) + MA120 여유. 전체 400일을 점수/zones 재현하면 클릭마다 느림.
_CHART_BARS = 280


def _chart_freshness(dates: list[str], market: str = "kospi") -> dict:
    """차트 UI용 시각 메타 — 종가 기준일 · parquet 파일 갱신 · 실시간 오버레이 시각."""
    as_of = dates[-1] if dates else None
    prices_file = store.US_PRICES_FILE if market == "us" else store.PRICES_FILE
    prices_updated = None
    try:
        if prices_file.exists():
            prices_updated = datetime.datetime.fromtimestamp(
                prices_file.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")
    except OSError:
        prices_updated = None
    live = store.live_status()
    live_updated = None
    if live.get("updated"):
        try:
            live_updated = datetime.datetime.fromtimestamp(
                float(live["updated"]), tz=datetime.timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            live_updated = None
    return {
        "as_of": as_of,
        "prices_updated": prices_updated,
        "live_on": bool(live.get("on")),
        "live_updated": live_updated,
        "bar_count": len(dates),
    }


@app.get("/api/signals/{ticker}/chart")
def signal_chart_get(ticker: str, market: str = "kospi"):
    """종목 가격+지표 시계열(차트용) — 종가/MA20·60·120/RSI/MACD. market=us면 미국 시세.
    최근 _CHART_BARS만 보내고, 점수·zones는 한 패스로 계산(클릭 지연 완화)."""
    history = store.load_us_price_history(ticker) if market == "us" else store.load_price_history(ticker)
    if not history:
        return {"ready": False, "dates": []}
    if len(history) > _CHART_BARS:
        history = history[-_CHART_BARS:]
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
    stored = store.signal_history_for(ticker) if market != "us" else {}  # 실측 시그널(PIT) 우선
    actual_dates = [d for d in dates if d in stored]
    scores, zones = chart_scores_and_zones(dates, closes, stored=stored)
    scores = _anchor_today_score(scores, ticker, market)
    # 일별 수급(KR만) — 차트 dates에 정렬. 없으면 null 배열(패널은 비움).
    # 주의: flow_foreign = flow_inst = [...] 는 같은 리스트를 공유하므로 절대 쓰지 말 것.
    n = len(dates)
    flow_foreign: list = [None] * n
    flow_inst: list = [None] * n
    if market != "us":
        try:
            from signal_desk.ingest import naver
            series_flow = naver.investor_flow_series(ticker, days=min(260, max(60, n)))
            if series_flow:
                by_d = {r["date"]: r for r in series_flow}
                flow_foreign = [(by_d[d]["foreign_net"] if d in by_d else None) for d in dates]
                flow_inst = [(by_d[d]["inst_net"] if d in by_d else None) for d in dates]
        except Exception as e:
            log.warning("차트 수급 패널 스킵(%s): %s", ticker, type(e).__name__)
    quote = None
    if market != "us":
        try:
            quote = _quotes().get(ticker)
        except Exception:
            quote = None
    return {
        "ready": True,
        "ticker": ticker,
        "quote": quote,  # US는 헤더 quote 별도 없음(현재가는 항목에)
        "dates": dates,
        "close": closes,
        "ma20": series["ma_short"],
        "ma60": series["ma_mid"],
        "ma120": series["ma_long"],
        "rsi": series["rsi"],
        "zones": zones,
        "scores": scores,
        "flow_foreign": flow_foreign,
        "flow_inst": flow_inst,
        "actual_from": actual_dates[0] if actual_dates else None,  # 이 날짜 이후는 실측(그 전은 재현)
        "macd": series["macd"]["macd"],
        "macd_signal": series["macd"]["signal"],
        "macd_hist": series["macd"]["histogram"],
        **_chart_freshness(dates, market=market),
    }


@app.get("/api/market/chart")
def market_chart_get():
    """코스피200 근사 지수 차트 — 시그널 탭 최상단 고정. 종목 차트와 동일하게 MA/RSI/MACD +
    매수/매도 구간 + 현재 시그널(가격기반)을 함께 준다."""
    history = store.load_index_history()
    if not history:
        return {"ready": False, "dates": []}
    if len(history) > _CHART_BARS:
        history = history[-_CHART_BARS:]
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
    cfg = SignalConfig()
    combined = combine(_price_only_components(closes, series, len(closes) - 1, cfg), cfg)
    scores, zones = chart_scores_and_zones(dates, closes)
    return {
        "ready": True, "ticker": "KOSPI200X", "name": "코스피200 지수(근사)",
        "dates": dates, "close": closes,
        "ma20": series["ma_short"], "ma60": series["ma_mid"], "ma120": series["ma_long"],
        "rsi": series["rsi"], "zones": zones,
        "scores": scores,
        "macd": series["macd"]["macd"], "macd_signal": series["macd"]["signal"], "macd_hist": series["macd"]["histogram"],
        "kind": combined["kind"], "score": combined["score"], "confidence": combined["confidence"],
        "reasons": combined["reasons"],
        **_chart_freshness(dates, market="kospi"),
    }


_DART_TTL_DAYS = 80  # DART 연간 재무는 분기에나 바뀜 → 이 주기로만 재수집(그 외엔 시총만 매일 재계산)


def _dart_stale(ttl_days: int = _DART_TTL_DAYS) -> bool:
    """DART 재무를 다시 받아야 하나 — 캐시 없거나 마지막 수집이 ttl_days 이상 지났으면 True."""
    if not store.load_fundamentals():
        return True
    last = db.kv_get("dart_fetch_date")
    if not last:
        return True
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(str(last))).days >= ttl_days
    except ValueError:
        return True


def _clear_signal_caches() -> None:
    """수집 후 파생 캐시 무효화 — 어느 scope를 돌려도 안전하게 매번 비운다."""
    _signals.cache_clear()
    _backtest.cache_clear()
    _backtest_analysis.cache_clear()
    _valuation.cache_clear()
    _quotes.cache_clear()
    _regime.cache_clear()
    _macro.cache_clear()
    _clear_us_signal_caches()


def _refresh_kr(data: dict) -> dict:
    """국내 유니버스+시세+재무(+PER/PBR·퀄리티·배당). DART 재무는 분기(≈80일)마다만 재수집하고
    (연간 데이터라 거의 불변), 그 외엔 시총만 다시 받아 매일 재계산. force_dart=true면 강제."""
    universe = store.fetch_universe()
    # 최초 1회는 5년 전량 백필(deep), 이후엔 증분. 관리자 '데이터 갱신' 버튼만 눌러도 처음엔 5년치를
    # 채우고(수 분 소요) 그다음부터는 마지막 저장일부터만 가볍게 갱신. full_prices=true면 강제 재백필.
    deep = bool(data.get("full_prices")) or not db.kv_get("prices_deep_backfilled")
    store.fetch_prices(universe, full=deep)
    if deep:
        db.kv_set("prices_deep_backfilled", _kst_today())
    if bool(data.get("force_dart")) or _dart_stale():
        fundamentals = store.fetch_fundamentals(universe)      # DART 재무 + PER/PBR (분기 1회)
        store.fetch_fundamentals_history(universe)             # point-in-time 백테스트용 연도별 재무
        store.compute_quality()                                # 당해+전년 → 축약 F-Score(퀄리티 팩터)
        try:
            store.fetch_kr_dividends(universe)                 # KR 주당배당(DART) → 배당 플래너
        except Exception as e:
            log.warning("KR 배당 수집 실패(무시): %s", type(e).__name__)
        try:
            store.fetch_company_profiles(universe)             # DART 기업개황(설립·대표) → 숏폼 기업 소개(증분)
        except Exception as e:
            log.warning("기업개황 수집 실패(무시): %s", type(e).__name__)
        db.kv_set("dart_fetch_date", _kst_today())
    else:
        store.update_valuation()                               # 캐시 재무 + 오늘 시총 → PER/PBR·시총만 갱신(KRX 1콜)
        fundamentals = store.load_fundamentals()
        log.info("DART 재무 최신(분기 내) — 재수집 스킵, 시총만 갱신")
    # 기업개황은 정적이라 DART 재무 게이트(≈80일)에 묶여 있으나, 이 항목이 나중에 추가돼 date-gate에
    # 막혀 백필이 안 되던 케이스 → 비어 있으면 게이트와 무관하게 1회 백필(증분·키 없으면 즉시 무동작).
    if not store.load_company_profiles():
        try:
            store.fetch_company_profiles(universe)
        except Exception as e:
            log.warning("기업개황 백필 실패(무시): %s", type(e).__name__)
    about_n = _backfill_about_batch(40)  # 사업 개요 LLM 증분 백필(국내 갱신에서도 채움)
    moves_n = _backfill_moves_batch(20)  # 최근 행보 LLM 증분 백필(KB 문서 있는 종목만)
    return {"universe_size": len(universe), "fundamentals_size": len(fundamentals),
            "about_generated": about_n, "moves_generated": moves_n}


def _refresh_macro(data: dict) -> dict:
    """거시(FRED)+한국은행 ECOS+토스 투자경고. 상대적으로 가벼운 그룹."""
    macro_items = store.fetch_macro()
    store.fetch_macro_kr()  # 한국은행 ECOS 거시(키 있을 때만 채워짐)
    try:
        store.fetch_warnings([u["ticker"] for u in store.load_universe()])  # 투자경고/거래정지/VI → 매수 veto
    except Exception as e:
        log.warning("토스 경고 수집 실패(무시): %s", type(e).__name__)
    try:
        mf = store.fetch_market_flow()  # 토스 시장전체(KOSPI) 외국인·기관 순매수 → 국면 신호(종목별 pykrx 대체)
    except Exception as e:
        log.warning("시장 수급 수집 실패(무시): %s", type(e).__name__)
        mf = {}
    return {"macro_size": len(macro_items), "market_flow": bool(mf)}


def _refresh_flows(data: dict) -> dict:
    """투자자별 수급(외국인·기관 순매수, KR) + 공매도 거래비중(KRX) → 수급·공매도 팩터."""
    out: dict = {}
    try:
        store.fetch_flows(store.load_universe())
        out["flows_size"] = len(store.load_flows())
    except Exception as e:
        log.warning("수급 수집 실패(무시): %s", type(e).__name__)
        out["flows_size"] = len(store.load_flows())
        out["flows_error"] = type(e).__name__
    try:
        store.fetch_short(store.load_universe())
        out["short_size"] = len(store.load_short())
    except Exception as e:
        log.warning("공매도 수집 실패(무시): %s", type(e).__name__)
        out["short_size"] = len(store.load_short())
        out["short_error"] = type(e).__name__
    return out


def _backfill_us_prices_batch(batch: int = 60) -> dict:
    """S&P500 중 아직 시세 없는 종목을 batch개만 백필(증분). us_prices.parquet은 gitignore라 배포
    환경에선 비어 있으므로, 갱신/백그라운드 루프가 눌릴 때마다 점진 적재해 전량을 채운다.
    반환: {filled, missing}(이번에 채운 수 / 백필 후 남은 수)."""
    universe = [u["ticker"] for u in store.load_us_universe()]
    if not universe:
        return {"filled": 0, "missing": 0}
    have = set(store.load_us_price_series().keys())
    missing = [t for t in universe if t not in have]
    if not missing:
        return {"filled": 0, "missing": 0}
    filled = store.fetch_us_prices(missing[:batch], days=400)
    return {"filled": filled, "missing": max(0, len(missing) - batch)}


def _about_targets_kr() -> list[dict]:
    return [{"ticker": u["ticker"], "name": u["name"], "sector": sectors.sector_of(u["ticker"]), "market": "kr"}
            for u in store.load_universe()]


def _about_targets_us() -> list[dict]:
    fund = store.load_us_fundamentals()
    return [{"ticker": u["ticker"], "name": us_ko.name_ko(u["ticker"], u["name"]),
             "sector": us_ko.sector_ko(u.get("sector")), "market": "us",
             "us_description": (fund.get(u["ticker"]) or {}).get("description")}
            for u in store.load_us_universe()]


def _backfill_about_batch(max_llm: int = 30) -> int:
    """국내+해외 '사업 개요'를 LLM으로 증분 백필(캐시 없는 종목만, 상한까지). LLM 없으면 0.
    요청 경로가 아니라 갱신·백그라운드에서만 호출(수백 종목 동기 LLM 방지)."""
    try:
        n = company.backfill(_about_targets_kr(), max_llm=max_llm)
        if n < max_llm:
            n += company.backfill(_about_targets_us(), max_llm=max_llm - n)
        return n
    except Exception as e:
        log.warning("사업 개요 백필 실패(무시): %s", type(e).__name__)
        return 0


def _backfill_moves_batch(max_llm: int = 15) -> int:
    """국내+해외 '최근 행보'를 KB 원자료 기반으로 증분 백필(KB 문서 있고 캐시가 오래된 종목만). LLM 없으면 0."""
    try:
        kr = [{"ticker": u["ticker"], "name": u["name"]} for u in store.load_universe()]
        n = company.backfill_moves(kr, max_llm=max_llm)
        if n < max_llm:
            us = [{"ticker": u["ticker"], "name": us_ko.name_ko(u["ticker"], u["name"])}
                  for u in store.load_us_universe()]
            n += company.backfill_moves(us, max_llm=max_llm - n)
        return n
    except Exception as e:
        log.warning("최근 행보 백필 실패(무시): %s", type(e).__name__)
        return 0


def _refresh_us(data: dict) -> dict:
    """미국: 거장 13F + S&P500 유니버스/발행주식수/EDGAR 재무(증분) + S&P500 시세(증분 백필)."""
    us_prices = {"filled": 0, "missing": None}
    try:
        store.fetch_gurus()  # 거장 포트폴리오(SEC 13F) — 실패해도 나머지 수집엔 영향 없음
        us_uni = store.fetch_us_universe()  # S&P500 유니버스
        us_all = [u["ticker"] for u in us_uni]
        store.fetch_us_shares_toss(us_all)  # 토스 발행주식수 → 전 종목 시총(AV 병목 없이)
        # US 재무(EDGAR 순이익·자기자본, 무료·무키) — 증분 백필(이미 채운 건 스킵). 갱신 누를 때마다 진행돼
        # 여러 번 누르면 S&P500 전량이 채워진다(한 번에 120종목, EDGAR 10req/s 여유).
        got = store.fetch_us_fundamentals_edgar(us_all, max_calls=120)
        log.info("US 재무(EDGAR) 백필 시도 %d종목", got)
        ec = store.fetch_us_earnings_calendar()  # 실적 예정 캘린더(AV 벌크 1콜/일, TTL로 절약)
        log.info("US 실적 예정 캘린더: %s", "신선(스킵)" if ec == -1 else f"{ec}종목")
        # S&P500 시세 증분 백필 — 시그널 노출의 핵심(시세 없으면 evaluate가 제외). 배포 환경은 캐시가
        # 비어 있으므로 갱신을 여러 번 누르면 전량이 채워진다(요청당 타임아웃 피하려 배치).
        us_prices = _backfill_us_prices_batch(int(data.get("us_price_batch") or 60))
        log.info("US 시세 증분 백필 %d종목(잔여 %s)", us_prices["filled"], us_prices["missing"])
        idx = gurus_ref.build_name_index(us_uni)  # 거장 보유종목(비 S&P500 포함) → 시세 수집(뱃지용, 스로틀)
        us_tks = sorted({t for g in store.load_gurus() for h in g.get("holdings", [])
                         if (t := gurus_ref.match_ticker(h.get("name", ""), idx))})
        extra = [t for t in us_tks if t not in {u["ticker"] for u in us_uni}]
        if extra:
            store.fetch_us_prices(extra)
    except Exception as e:
        log.warning("거장/US 수집 실패(무시): %s", e)
    us_fund = store.load_us_fundamentals()
    us_filled = sum(1 for f in us_fund.values() if f.get("net_income") is not None or f.get("equity") is not None)
    about_n = _backfill_about_batch(40)  # 사업 개요 LLM 증분 백필(국내+해외, 캐시 없는 종목만)
    moves_n = _backfill_moves_batch(20)  # 최근 행보 LLM 증분 백필(KB 문서 있는 종목만)
    return {"us_fund_filled": us_filled, "us_universe_size": len(us_fund) or None,
            "us_prices_filled": us_prices["filled"], "us_prices_missing": us_prices["missing"],
            "about_generated": about_n, "moves_generated": moves_n}


def _refresh_consensus(data: dict) -> dict:
    """애널 컨센서스(목표주가·투자의견·선행EPS) PIT 스냅샷 축적 — 리비전/목표가v2용(아직 미반영)."""
    try:
        n = store.fetch_consensus(store.load_universe())
    except Exception as e:
        log.warning("컨센서스 수집 실패(무시): %s", type(e).__name__)
        return {"consensus_snapshot_error": type(e).__name__}
    hist = store.load_consensus_history()
    return {"consensus_snapshot_rows": n,
            "consensus_days_accumulated": int(hist["date"].nunique()) if not hist.empty else 0}


_REFRESH_RUNNERS = {"kr": _refresh_kr, "macro": _refresh_macro, "flows": _refresh_flows,
                    "us": _refresh_us, "consensus": _refresh_consensus}


@app.post("/api/refresh")
def refresh(data: dict = Body(default={})):
    """데이터 재수집 + 파생 캐시 무효화. scope로 분할 호출해 요청당 타임아웃을 피한다:
    kr(시세·재무·배당) / macro(거시·경고) / flows(수급) / us(EDGAR 등). scope 미지정=all(전부, 하위호환)."""
    scope = str(data.get("scope") or "all").lower()
    result: dict = {"ok": True, "scope": scope}
    if scope == "all":
        errors = {}
        # consensus는 무겁고(종목당 2콜) 마감후 루프에서 자동 축적되므로 all에선 제외 — 명시적 scope로만.
        for name, fn in _REFRESH_RUNNERS.items():
            if name == "consensus":
                continue
            try:
                result.update(fn(data))
            except Exception as e:  # scope 하나가 죽어도 나머지는 계속 (부분 수집)
                log.exception("refresh scope=%s 실패", name)
                errors[name] = f"{type(e).__name__}: {e}"
        if errors:
            result["ok"] = False
            result["errors"] = errors
    elif scope in _REFRESH_RUNNERS:
        try:
            result.update(_REFRESH_RUNNERS[scope](data))
        except Exception as e:
            log.exception("refresh scope=%s 실패", scope)
            return {"ok": False, "scope": scope, "error": f"{type(e).__name__}: {e}"}
    else:
        return {"ok": False, "reason": f"알 수 없는 scope: {scope} (kr|macro|flows|us|all)"}
    _clear_signal_caches()
    return result


@app.get("/api/valuation")
def valuation_get():
    """PER/PBR 저평가 순위(0=가장 저평가) — signals/valuation.py 참고. 섹터 분류 붙기 전까지는
    전체 유니버스 내 상대 순위로 근사."""
    if not store.is_ready():
        return {"ready": False, "items": []}
    return {"ready": True, "items": _valuation()}


@app.get("/api/regime")
def regime_get():
    """시장 국면(강세·과열·조정·약세) — signals/regime.py 참고. 유니버스 breadth+모멘텀 근사."""
    if not store.is_ready():
        return {"ready": False, "regime": None}
    mf_raw = store.load_market_flow()
    _, adapt = signalcfg.effective_config(_regime(), _macro(), flow_result=mf_raw)  # 국면 적응으로 상향된 매수 기준
    flow = regime.market_flow_bias(mf_raw)  # 토스 시장전체 외국인·기관 순매수 방향
    return {**_regime(), "adaptive": adapt, "market_flow": flow}


@app.get("/api/egress-ip")
def egress_ip_get():
    """서버의 아웃바운드(공인) IP — 토스 등 외부 API IP 화이트리스트 등록용. 여러 소스로 시도.
    ⚠️ Railway 등은 배포/인스턴스마다 이 IP가 바뀔 수 있음(고정 egress 아니면 화이트리스트가 깨짐)."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode("utf-8", "replace").strip()
            if ip:
                return {"ok": True, "ip": ip, "source": url,
                        "note": "Railway는 배포마다 IP가 바뀔 수 있어 화이트리스트가 깨질 수 있습니다(고정 egress 확인)."}
        except Exception:
            continue
    return {"ok": False, "reason": "아웃바운드 IP 조회 실패(외부 IP 서비스 모두 응답 없음)"}


@app.get("/api/dividends")
def dividends_get(market: str = "us"):
    """배당주 리스트(배당 플래너) — 배당수익률·주당배당·현재가 + 시그널·시총·섹터. 수익률 내림차순.
    market=us(EDGAR TTM, 월배당 가능) | kr(DART 결산배당, 연1회≈4월). 봇과 분리된 '현금흐름' 도구."""
    if _mkt(market) == "us":
        divs, currency = store.us_dividends(), "USD"
        sig = _us_signals()
        mcaps = store.us_marketcaps()
        names = {u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()}
        sec_of = lambda t: us_ko.sector_ko({u["ticker"]: u.get("sector") for u in store.load_us_universe()}.get(t))
    else:
        divs, currency = store.kr_dividends(), "KRW"
        sig = {s.ticker: s for s in _signals()} if store.is_ready() else {}
        quotes = _quotes()
        mcaps = {t: {"mktcap": (q or {}).get("mktcap")} for t, q in quotes.items()}
        names = {u["ticker"]: u["name"] for u in store.load_universe()}
        sec_of = lambda t: sectors.sector_of(t)
    if not divs:
        msg = ("배당 데이터 없음 — 관리자 데이터 갱신 필요"
               + (" (EDGAR 배당 백필)" if _mkt(market) == "us" else " (DART 배당)"))
        return {"ready": False, "items": [], "currency": currency, "message": msg}
    items = []
    for t, d in divs.items():
        s = sig.get(t)
        items.append({"ticker": t, "name": names.get(t, t), "price": d["price"],
                      "dps": d["dps"], "div_yield": d["div_yield"], "div_months": d.get("div_months") or [],
                      "kind": s.kind if s else None, "score": round(s.score, 2) if s else None,
                      "mktcap": (mcaps.get(t) or {}).get("mktcap"), "sector": sec_of(t)})
    items.sort(key=lambda x: (x["div_yield"] or 0, x["mktcap"] or 0), reverse=True)
    return {"ready": True, "currency": currency, "market": _mkt(market), "items": items}


@app.get("/api/data-health")
def data_health_get():
    """데이터 진단(관리자) — 시세 스케일 정합(price_sanity) + 소스별 신선도(마지막 갱신·경과·stale).
    track record 신뢰의 전제(실데이터) + 어떤 소스가 오래됐는지 한눈에."""
    fresh = store.data_freshness()
    digests = db.kb_digests_all()
    if digests:  # KB 다이제스트 신선도(최신 갱신 기준)
        latest = max((d.get("updated") or 0) for d in digests.values())
        age_h = (time.time() - latest) / 3600 if latest else None
        fresh.append({"key": "kb", "label": "KB 다이제스트", "rows": len(digests),
                      "updated": (datetime.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
                                  if latest else None),
                      "age_hours": round(age_h, 1) if age_h is not None else None,
                      "stale": age_h is None or age_h > 48})
    return {**store.price_sanity(), "freshness": fresh}


@app.get("/api/live-status")
def live_status_get():
    """실시간가 오버레이 상태 — 현재가가 언제 갱신됐는지·토스 연동·장중 여부 진단용."""
    from signal_desk.ingest import toss
    return {"toss": toss.available(), "kr_open": bot.is_market_hours(),
            "us_open": bot.is_us_market_hours(), **store.live_status()}


# ---------- 자동매매봇 (유저별 자체 모의계좌 · 공용 시그널 · 시장별 kr/us) ----------
def _mkt(v) -> str:
    return "us" if str(v or "kr").lower() == "us" else "kr"


@app.get("/api/bot/state")
def bot_state_get(request: Request, market: str = "kr"):
    return bot.get_state(_uid(request), _mkt(market))


@app.get("/api/bot/performance")
def bot_performance_get(request: Request, market: str = "kr"):
    """내 봇 track record — 자산곡선 + 총수익률·최대낙폭·거래수."""
    return bot.performance(_uid(request), _mkt(market))


@app.get("/api/reference-performance")
def reference_performance_get(market: str = "kr"):
    """공용 레퍼런스 봇(성향별) track record — 시그널 신뢰의 공개 증거."""
    return bot.reference_performance(_mkt(market))


@app.post("/api/bot/toggle")
def bot_toggle(request: Request, data: dict = Body(...)):
    bot.set_enabled(_uid(request), bool(data.get("enabled")))
    return {"ok": True, "enabled": bool(data.get("enabled"))}


@app.post("/api/bot/style")
def bot_style(request: Request, data: dict = Body(...)):
    """내 봇 트레이딩 성향(안정형/균형형/공격형) 변경 — 파라미터·리스크 룰이 프리셋으로 바뀐다."""
    style = bot.set_style(_uid(request), str(data.get("style", "balanced")))
    return {"ok": True, "style": style}


@app.post("/api/bot/seed")
def bot_seed(request: Request, data: dict = Body(...)):
    """내 봇 초기 시드 금액 설정(시장별, 다음 초기화 때 반영)."""
    try:
        seed = float(data.get("seed_cash") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "금액 오류"}
    if seed <= 0:
        return {"ok": False, "reason": "0보다 큰 금액을 입력하세요."}
    bot.set_seed(_uid(request), seed, _mkt(data.get("market")))
    return {"ok": True, "seed_cash": seed}


@app.post("/api/bot/run")
def bot_run(request: Request, data: dict = Body(default={})):
    """내 봇 수동 1회 실행(시장별) — 자체 모의계좌 종가 기준 가상 체결."""
    return bot.run_once(_uid(request), dry_run=False, market=_mkt(data.get("market")))


@app.get("/api/bot/us/state")
def bot_us_state(capital: float = 10000.0):
    """해외(US) 대시보드 상태 — 잔고(USD)·보유종목 + 판단 미리보기(국내와 동일 레이아웃)."""
    return bot.us_state(capital=capital)


@app.post("/api/bot/us/preview")
def bot_us_preview(data: dict = Body(default={})):
    """US 자동매매 판단 미리보기(주문 없음) — US 시그널+KB 기반 매수 후보·분할 계획(USD).
    실주문·잔고는 미국장 개장 시 KIS 해외 API 검증 후 연결 예정."""
    try:
        capital = float(data.get("capital") or 10000)
    except (TypeError, ValueError):
        capital = 10000.0
    return bot.us_preview(capital=capital, style=data.get("style"))


@app.post("/api/bot/preview")
def bot_preview(request: Request, data: dict = Body(default={})):
    """내 봇 판단 미리보기(dry-run, 시장별) — 주문 없이 '지금 무엇을 왜 매매할지' 계획만."""
    return bot.run_once(_uid(request), dry_run=True, market=_mkt(data.get("market")))


@app.post("/api/bot/reset")
def bot_reset(request: Request):
    """내 봇 초기화 — 포지션·거래·예약 삭제 + 페이퍼 현금 시드로 리셋."""
    bot.reset(_uid(request))
    return {"ok": True}


@app.post("/api/bot/reserve")
def bot_reserve(request: Request, data: dict = Body(default={})):
    """내 봇 예약 주문 생성(수동 트리거, 시장별). dry_run이면 계획만."""
    return bot.generate_reservations(_uid(request), dry_run=bool(data.get("dry_run")), market=_mkt(data.get("market")))


@app.post("/api/bot/execute-reservations")
def bot_execute_reservations(request: Request, data: dict = Body(default={})):
    """내 봇 대기 예약을 지금 실행(수동 트리거, 시장별). dry_run이면 계획만."""
    return bot.execute_reservations(_uid(request), dry_run=bool(data.get("dry_run")), market=_mkt(data.get("market")))


@app.get("/api/bot/decisions")
def bot_decisions_get():
    """의사결정 저널(학습 기록) — 최근 결정 + 사후수익."""
    return {"decisions": db.bot_decisions_recent(40)}


# ---------- KB (뉴스·영상 → 정성 다이제스트) ----------
def _kb_targets(limit_candidates: int = 12, lead_limit: int = 10) -> list[dict]:
    """KB 갱신 대상 — ⓪외부 후보 ①lead ②VIX soft ③BUY ④보유 ⑤관심.
    전 종목 아님. 외부 후보는 조사 큐 우선(점수 가산 없음)."""
    from signal_desk import external_watch
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    for u in store.load_us_universe() or []:
        names.setdefault(u["ticker"], us_ko.name_ko(u["ticker"], u.get("name") or u["ticker"]))
    targets, seen = [], set()

    def add(ticker, name=None):
        if ticker in seen:
            return
        if ticker in names:
            targets.append({"ticker": ticker, "name": names[ticker]})
            seen.add(ticker)
        elif name:
            targets.append({"ticker": ticker, "name": name})
            seen.add(ticker)

    # ⓪ 외부 후보 워치리스트 (Serenity 등) — 맨 앞
    try:
        for row in external_watch.kb_priority_targets():
            add(row["ticker"], row.get("name"))
    except Exception as e:
        log.warning("KB 외부후보 타깃 실패: %s", type(e).__name__)

    # ① 확정 국면 주도 섹터
    try:
        pos = cycle.position(store.load_macro())
        for row in valuechain.tickers_for_lead_tags(pos.get("lead_sectors") or [], limit=lead_limit):
            add(row["ticker"], row.get("name"))
        risk = cycle.risk_sentiment(store.load_macro())
        hint = risk.get("kb_hint_phase_key")
        if hint and hint != pos.get("phase_key"):
            for row in valuechain.tickers_for_lead_tags(
                    cycle.lead_sectors_for(hint), limit=4):
                add(row["ticker"], row.get("name"))
    except Exception as e:
        log.warning("KB lead 타깃 실패: %s", type(e).__name__)

    # ③ BUY 상위
    buy_n = 0
    if store.is_ready():
        for s in _signals():
            if s.kind == "BUY" and buy_n < limit_candidates:
                add(s.ticker)
                buy_n += 1
    for tk in db.bot_position_tickers_all():
        add(tk)
    for tk in db.fav_tickers_all():
        add(tk)
    return targets


@app.post("/api/kb/refresh")
def kb_refresh():
    """뉴스·영상 수집 → LLM 다이제스트 → KB 적재(대상: 보유+상위 BUY 후보). 시그널 캐시 무효화."""
    from signal_desk import external_watch
    targets = _kb_targets()
    if not targets:
        return {"ok": False, "reason": "대상 종목 없음 — /api/refresh로 유니버스 먼저 수집"}
    out = kb.refresh(targets)
    try:
        ext = external_watch.ticker_set()
        hit = [t["ticker"] for t in targets if t.get("ticker") in ext]
        if hit:
            external_watch.mark_kb_collected(hit)
    except Exception as e:
        log.warning("external_watch KB 마킹 실패: %s", type(e).__name__)
    _signals.cache_clear()  # 정성 팩터 반영 위해
    return {"ok": True, **out, "targets": len(targets)}


@app.post("/api/kb/collect-fanding")
def kb_collect_fanding(data: dict = Body(default={})):
    """fanding.kr 미주은 포스트 → KB 적재(수동 트리거). backfill_days>0이면 그 일수 이전까지 백필."""
    out = kb.collect_fanding(force=bool(data.get("force")),
                             backfill_days=int(data.get("backfill_days", 0)))
    if out.get("ok") and out.get("imported"):
        _signals.cache_clear()  # 새 정성 인사이트 반영
    if out.get("ok") and out.get("macro"):
        _macro.cache_clear()  # 시황 내러티브 갱신 반영(전광판·자문)
    return out


@app.post("/api/kb/collect-outstanding")
def kb_collect_outstanding(data: dict = Body(default={})):
    """아웃스탠딩 화이트리스트 작가 최신 기고 → 거시 KB(상장사 특정 글은 종목 KB) 적재(수동 트리거)."""
    n = int(data.get("item_per_page", 15))
    out = kb.collect_outstanding(item_per_page=n, force=bool(data.get("force")))
    if out.get("ok") and out.get("imported"):
        _signals.cache_clear()
    if out.get("ok") and out.get("macro"):
        _macro.cache_clear()
    return out


@app.post("/api/kb/collect-youtube")
def kb_collect_youtube(data: dict = Body(default={})):
    """유튜브 화이트리스트 채널 최신 영상(자막 전문) → 거시 KB(상장사 특정 영상은 종목 KB) 적재.
    max_per_channel 미지정 시 config.youtube_max_per_channel(env) 사용."""
    n = data.get("max_per_channel")
    out = kb.collect_youtube(max_per_channel=int(n) if n else None, force=bool(data.get("force")))
    if out.get("ok") and out.get("imported"):
        _signals.cache_clear()
    if out.get("ok") and out.get("macro"):
        _macro.cache_clear()
    return out


@app.post("/api/kb/collect-rss")
def kb_collect_rss(data: dict = Body(default={})):
    """해외 전문가·기관 RSS 화이트리스트(config.macro_rss_feeds) 최신 글 → 거시 KB 요약 적재(수동 트리거)."""
    n = data.get("limit_per_feed")
    out = kb.collect_rss_macro(force=bool(data.get("force")), limit_per_feed=int(n) if n else None)
    if out.get("ok") and out.get("macro"):
        _macro.cache_clear()
    return out


# ---------- 숏폼 콘텐츠 (관리자 전용 · 생성→검수→발행) ----------
def _admin_or_403(request: Request):
    if not _require_admin(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")


@app.get("/api/shortform/candidates")
def shortform_candidates(limit: int = 20):
    """숏폼 후보 — 매수 시그널 점수순 + 근거(선택 전 단계, 생성 안 함)."""
    return {"candidates": shortform.candidates(limit=limit)}


@app.post("/api/shortform/generate")
def shortform_generate(data: dict = Body(default={})):
    """선택한 종목(tickers)으로 숏폼 초안(스크립트+카드) 생성 → 검수 큐 적재. tickers 없으면 상위 자동."""
    tickers = data.get("tickers")
    tickers = [str(t) for t in tickers] if isinstance(tickers, list) else None
    return shortform.generate(tickers=tickers, limit=int(data.get("limit", 5)),
                              dry_run=bool(data.get("dry_run")))


@app.post("/api/shortform/generate-performance")
def shortform_generate_performance(data: dict = Body(default={})):
    """레퍼런스 봇 성과(track record)를 숏폼 초안으로 → 검수 큐. style: conservative|balanced|aggressive."""
    return shortform.generate_performance(style=str(data.get("style") or "balanced"),
                                          market=_mkt(data.get("market")))


@app.get("/api/shortform/queue")
def shortform_queue(status: str | None = None):
    """검수 큐 목록(카드 SVG 제외, 가벼움). status=draft|approved|rejected|published."""
    return {"items": db.shortform_list(status=status)}


@app.get("/api/shortform/background")
def shortform_bg_get(request: Request):
    """카드 배경 이미지 URL 조회(관리자). '' = 미설정(단색 배경)."""
    _admin_or_403(request)
    return {"url": db.kv_get("shortform_bg") or ""}


@app.post("/api/shortform/background")
def shortform_bg_set(request: Request, data: dict = Body(default={})):
    """카드 배경 이미지 URL 설정(관리자). 외부 호스팅 URL(http/https) 또는 우리가 서빙하는
    업로드 URL. data URI는 장면 SVG마다 박혀 DB가 커지므로 거부(업로드는 아래 -upload로)."""
    _admin_or_403(request)
    url = str(data.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://", "/api/")):
        return {"ok": False, "reason": "http(s) URL만 허용 — 로컬 파일은 '이미지 업로드'를 쓰세요(data URI는 DB 부담)."}
    db.kv_set("shortform_bg", url or None)
    return {"ok": True, "url": url}


@app.post("/api/shortform/background-upload")
async def shortform_bg_upload(request: Request, file: UploadFile = FastFile(...)):
    """로컬 이미지 업로드 → 서버에 1장 저장 → 짧은 앱 URL을 배경으로 설정(관리자).
    data URI를 장면마다 박지 않으므로 DB 부담 없음. 상업 이용 라이선스는 사용자 책임."""
    _admin_or_403(request)
    media_type = file.content_type or ""
    if not media_type.startswith("image/"):
        return {"ok": False, "reason": f"이미지 파일만 업로드 가능({media_type or '알 수 없음'})"}
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        return {"ok": False, "reason": "배경 이미지는 최대 5MB"}
    store.save_shortform_bg(data)
    db.kv_set("shortform_bg_mime", media_type)
    url = f"/api/shortform/background-image?v={int(time.time())}"  # 캐시버스트
    db.kv_set("shortform_bg", url)
    return {"ok": True, "url": url}


@app.get("/api/shortform/background-image")
def shortform_bg_image():
    """업로드된 배경 이미지 원본 서빙(장면 SVG의 <image>가 참조). 없으면 404."""
    path = store.shortform_bg_path()
    if not path:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="배경 이미지 없음")
    return FileResponse(path, media_type=db.kv_get("shortform_bg_mime") or "image/jpeg")


@app.post("/api/shortform/tts-test")
def shortform_tts_test(request: Request, data: dict = Body(default={})):
    """Typecast TTS 연결 확인(관리자) — 텍스트를 합성해 mp3로 바로 반환(브라우저 재생). 키는 .env."""
    _admin_or_403(request)
    from fastapi.responses import Response
    from signal_desk.ingest import typecast
    if not typecast.available():
        return JSONResponse({"ok": False, "reason": "TYPECAST_API_KEY 미설정(.env에 추가하세요)"}, status_code=400)
    text = str(data.get("text") or "안녕하세요. 오늘의 시그널입니다.").strip()
    audio = typecast.synthesize(text)
    if not audio:
        return JSONResponse({"ok": False, "reason": "TTS 합성 실패 — 키·쿼터·네트워크를 확인하세요"}, status_code=502)
    return Response(content=audio, media_type="audio/mpeg")


@app.get("/api/shortform/{sid}")
def shortform_detail(sid: str, request: Request):
    """단건 상세(스크립트 + 카드 SVG 포함) — 검수 미리보기용."""
    _admin_or_403(request)
    item = db.shortform_get(sid)
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="숏폼을 찾을 수 없습니다.")
    return item


@app.post("/api/shortform/{sid}/review")
def shortform_review(sid: str, request: Request, data: dict = Body(default={})):
    """검수 결과 반영 — status: approved|rejected(|published). note 선택."""
    _admin_or_403(request)
    status = str(data.get("status") or "").strip()
    if status not in ("approved", "rejected", "published"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="status는 approved|rejected|published")
    db.shortform_set_status(sid, status, str(data.get("note") or ""))
    return {"ok": True, "id": sid, "status": status}


@app.post("/api/shortform/{sid}/delete")
def shortform_delete_ep(sid: str, request: Request):
    _admin_or_403(request)
    db.shortform_delete(sid)
    return {"ok": True, "id": sid}


@app.get("/api/shortform/{sid}/export")
def shortform_export_ep(sid: str, request: Request):
    """로컬 렌더용 zip 다운로드(관리자) — 서버는 렌더하지 않고 자료(장면 SVG·나레이션·폰트·render.py)만
    zip으로. PC에서 render.py 실행해 mp4 생성. 파일명은 종목명_종목코드.zip."""
    _admin_or_403(request)
    import urllib.parse

    from fastapi.responses import Response
    from signal_desk import shortform_render
    out = shortform_render.export(sid)
    if not out:
        return JSONResponse({"ok": False, "reason": "장면이 없는 초안(재생성 필요)"}, status_code=404)
    data, fname = out
    # 한글 파일명은 RFC5987(filename*)로 — 브라우저 호환
    quoted = urllib.parse.quote(fname)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})


# 주의: 아래 구체 경로들은 catch-all `/api/kb/{ticker}`보다 먼저 등록돼야 매칭된다.
@app.get("/api/kb/documents")
def kb_documents_get(ticker: str | None = None, doc_class: str | None = None, limit: int = 120):
    """KB 문서 목록(관리자 대시보드) — 유형·종목 필터 + 유형별 건수."""
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names[kb.MACRO_TICKER] = kb.MACRO_NAME  # 거시 내러티브 가상 종목
    docs = db.kb_documents(ticker, doc_class, limit)
    for d in docs:
        d["name"] = names.get(d["ticker"], d["ticker"])
    return {"documents": docs, "class_counts": db.kb_class_counts(), "classes": list(kb.DOC_CLASSES)}


@app.get("/api/kb/events")
def kb_events_get(ticker: str | None = None, limit: int = 50, active: bool = False,
                  view: str = "eligible"):
    """구조화 KB 이벤트 카드(읽기) — Decision 입력·감사. 점수 가산 아님.
    view=eligible(기본): 활성 confirmed · view=candidate: Sonnet 후보(Decision 미반영)
    · view=all: 최근 목록. active 쿼리는 레거시 호환(무시하고 eligible=활성 confirmed)."""
    v = (view or "eligible").lower()
    if v == "candidate":
        items = db.kb_events_list(limit=limit, ticker=ticker, status="candidate")
        policy = "p1b"
    elif v == "all":
        items = db.kb_events_list(limit=limit, ticker=ticker)
        policy = "p1b"
    else:
        items = db.kb_events_active(ticker)  # confirmed · 미만료 (active 플래그 포함)
        policy = "p0"
    for it in items:
        it["evidence"] = db.kb_event_evidence(it["id"])
    return {"items": items, "view": v if v in ("eligible", "candidate", "all") else "eligible",
            "policy_version": policy}


@app.get("/api/kb/sources")
def kb_sources_get(request: Request, lifecycle: str | None = None):
    """KB 수집 소스 레지스트리(관리자 읽기) — tier·수습·퇴출후보·최근 수집."""
    _admin_or_403(request)
    srcs = db.kb_sources_list(lifecycle=lifecycle or None)
    counts = {"all": 0, "probation": 0, "eviction_candidate": 0, "active": 0}
    for s in db.kb_sources_list():
        counts["all"] += 1
        life = s.get("lifecycle") or "active"
        if life in counts:
            counts[life] += 1
    return {"sources": srcs, "counts": counts, "policy_version": "p1.1"}


@app.post("/api/kb/sources/lifecycle")
def kb_sources_lifecycle(request: Request, data: dict = Body(...)):
    """채널/피드 수습·퇴출 조치 — pin|unpin|keep|evict|reprobation.
    자동 disable 없음. evict만 enabled=0."""
    _admin_or_403(request)
    key = str((data or {}).get("source_key") or "").strip()
    action = str((data or {}).get("action") or "").strip().lower()
    if not key or action not in ("pin", "unpin", "keep", "evict", "reprobation"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="source_key + action(pin|unpin|keep|evict|reprobation) 필요")
    out = db.kb_source_lifecycle_action(key, action)
    if not out.get("ok"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=out.get("reason") or "실패")
    return out

@app.get("/api/kb/digests")
def kb_digests_get():
    """종목별/거시 요약 다이제스트(관리자) — 원문이 아니라 LLM으로 종합·축약된 것.
    시그널·자문·해설이 실제로 소비하는 건 이 요약뿐(원문은 요약 생성 때만 사용)."""
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names[kb.MACRO_TICKER] = kb.MACRO_NAME
    out = []
    for ticker, dg in db.kb_digests_all().items():
        out.append({
            "ticker": ticker, "name": names.get(ticker, dg.get("name") or ticker),
            "summary": dg.get("summary"), "points": dg.get("points") or [],
            "sentiment": dg.get("sentiment"), "n_sources": dg.get("n_sources"),
            "newest_ts": dg.get("newest_ts"), "event_flag": dg.get("event_flag"),
            "is_macro": ticker.startswith("_"),
        })
    # 거시 먼저, 그다음 최신 원자료순
    out.sort(key=lambda x: (not x["is_macro"], -(x["newest_ts"] or 0)))
    return {"digests": out}


@app.post("/api/kb/import")
def kb_import(data: dict = Body(...)):
    """증권사 리포트·원문 텍스트를 KB 문서로 추가(LLM 요약·분류). {ticker, text, title?, source_type?, url?}."""
    ticker = (data.get("ticker") or "").strip()
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    name = names.get(ticker) or (data.get("name") or "").strip()
    if not ticker or not name:
        return {"ok": False, "reason": "유니버스에 없는 종목코드입니다(ticker 확인)"}
    out = kb.import_document(ticker, name, data.get("title", ""), data.get("text", ""),
                            data.get("source_type", "report"), data.get("url", ""))
    if out.get("ok"):
        _signals.cache_clear()
    return out


_UPLOAD_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_UPLOAD = 15 * 1024 * 1024  # 15MB


@app.post("/api/kb/import-file")
async def kb_import_file(ticker: str = Form(""), file: UploadFile = FastFile(...)):
    """PDF·이미지 업로드 → 요약·분류 후 KB 적재. ticker는 선택 — 비우면 문서 내용으로 종목/시황/섹터
    자동 분류·라우팅. 지정 시 해당 종목에 강제 적재(유니버스 코드여야 함)."""
    ticker = (ticker or "").strip()
    name = ""
    if ticker:  # 명시 지정 시에만 유니버스 검증(자동 모드는 kb가 판단)
        names = {u["ticker"]: u["name"] for u in store.load_universe()}
        name = names.get(ticker)
        if not name:
            return {"ok": False, "reason": "유니버스에 없는 종목코드입니다(ticker 확인 — 비우면 자동 분류)"}
    media_type = file.content_type or ""
    if media_type not in _UPLOAD_TYPES:
        return {"ok": False, "reason": f"지원 형식 아님({media_type}) — PDF·PNG·JPG만"}
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        return {"ok": False, "reason": "파일이 너무 큽니다(최대 15MB)"}
    out = kb.import_file(ticker or None, name, file.filename or "", data, media_type)
    if out.get("ok"):
        _signals.cache_clear()
        _macro.cache_clear()  # 시황/섹터로 라우팅됐을 수 있음
    return out


@app.get("/api/kb/{ticker}")
def kb_get(ticker: str):
    """종목 KB 다이제스트 + 최근 원자료 헤드라인."""
    return {"digest": db.kb_digest_get(ticker), "entries": db.kb_entries_recent(ticker, 8)}


# ---------- 사이클 / 밸류체인 (큐레이션 + FRED 현재위치) ----------
@app.get("/api/hypothesis")
def hypothesis_get():
    """최근 이슈 흐름 트리. 캐시만 — 없으면 ready:false. 자동 LLM/생성 없음."""
    return hypothesis.get(build_if_missing=False)


@app.post("/api/hypothesis/refresh")
def hypothesis_refresh(request: Request):
    """최근 이슈 흐름 수동 생성(Sonnet+룰) — 관리자 전용. 유일한 생성 경로."""
    _admin_or_403(request)
    return hypothesis.refresh()


@app.get("/api/climate-shadow")
def climate_shadow_get(request: Request):
    """기후 vs 기존 kind 일별 shadow 요약 — 관측용 · 봇/문턱 미연동. 관리자."""
    _admin_or_403(request)
    return climate.shadow_summary()


@app.get("/api/external-watch")
def external_watch_get(request: Request):
    """외부 후보 조사 큐 — 관리자. 시그널 점수 가산 없음."""
    _admin_or_403(request)
    from signal_desk import external_watch
    return external_watch.status()


@app.post("/api/external-watch")
def external_watch_add(request: Request, data: dict = Body(default={})):
    """조사 후보 일괄 추가(수동). body: {text|lines, note?} — 출처 크롤링 없음."""
    _admin_or_403(request)
    from signal_desk import external_watch
    raw = data.get("text") or data.get("lines") or ""
    return external_watch.add_items(
        raw, source="manual",
        note=str(data.get("note") or "").strip(),
        url=str(data.get("url") or "").strip())


@app.delete("/api/external-watch/{ticker}")
def external_watch_remove(request: Request, ticker: str):
    _admin_or_403(request)
    from signal_desk import external_watch
    return external_watch.remove(ticker)


@app.post("/api/external-watch/clear")
def external_watch_clear(request: Request):
    _admin_or_403(request)
    from signal_desk import external_watch
    return external_watch.clear()


@app.post("/api/external-watch/refresh-kb")
def external_watch_refresh_kb(request: Request):
    """외부 후보 우선으로 KB 뉴스 갱신(관리자). 일반 /api/kb/refresh와 동일 파이프라인."""
    _admin_or_403(request)
    return kb_refresh()


@app.get("/api/cycle")
def cycle_get():
    """경기 사이클 4국면 + 국면별 주도섹터, 현재 위치(FRED 거시 + 7일 히스테리시스 확정).
    각 주도섹터에 밸류체인 섹터 key(vc_key)를 달아 밸류체인 탭과 연결한다."""
    phases = []
    for p in cycle.phases():
        leads = [{"name": s, "vc_key": valuechain.key_for_tag(s)} for s in p["lead_sectors"]]
        phases.append({**p, "lead_sectors": leads})
    ind = _macro()["indicators"]
    cur = cycle.position(ind)
    # lead에 vc_key 부착(프론트 딥링크)
    cur = {**cur, "lead_sectors": [
        {"name": s, "vc_key": valuechain.key_for_tag(s)} for s in (cur.get("lead_sectors") or [])
    ]}
    risk = cycle.risk_sentiment(ind)
    return {"phases": phases, "current": cur, "risk_sentiment": risk}


@app.get("/api/glossary")
def glossary_get():
    """투자 용어·지표 학습 사전(스터디) — 카테고리별 개념/쉬운설명/왜보는지/우리시그널에서."""
    return {"categories": glossary.categories()}


@app.get("/api/valuechain")
def valuechain_get():
    """섹터별 밸류체인(업→다운스트림) 대표기업 큐레이션. 국내는 티커로 시그널 연결 가능.
    확정 경기국면(cycle)에 유리한 밸류체인을 cycle_fit로 태깅 — 사이클×밸류체인×시그널 내러티브."""
    pos = cycle.position(store.load_macro())
    leads = set(pos.get("lead_sectors") or [])
    secs = []
    for s in valuechain.sectors():  # 모듈 상수 변형 방지 위해 얕은 복사 후 태깅
        d = dict(s)
        d["cycle_fit"] = "favored" if leads & set(s.get("tags", [])) else "neutral"
        secs.append(d)
    if leads:  # 유리 국면 체인을 앞으로(신호 있는 유리 섹터부터 보이게)
        secs.sort(key=lambda x: x["cycle_fit"] != "favored")
    return {"sectors": secs, "cycle": {
        "ready": pos.get("ready"), "phase_name": pos.get("phase_name"),
        "phase_key": pos.get("phase_key"),
        "raw_phase_key": pos.get("raw_phase_key"),
        "stable": pos.get("stable"),
        "pending_phase_key": pos.get("pending_phase_key"),
        "pending_days": pos.get("pending_days"),
        "confirm_days": pos.get("confirm_days"),
        "lead_sectors": pos.get("lead_sectors") or [],
        "reasons": pos.get("reasons") or []}}


@lru_cache(maxsize=1)
def _us_signals():
    """미국 종목 시그널 — US 유니버스 중 시세 있는 종목. EDGAR 재무(PER/PBR)가 있으면 저평가 팩터도
    반영, 없으면 자동 제외. KB 감성(미주은 등)은 정성 팩터. 반환: {ticker: SignalResult}."""
    prices = store.load_us_price_series()
    if not prices:
        return {}
    fundamentals = {t: mc for t, mc in store.us_marketcaps(prices).items() if mc.get("per") or mc.get("pbr")}
    return {s.ticker: s for s in evaluate(store.load_us_universe(), prices,
                                          fundamentals=fundamentals, sentiment=kb.sentiment_map(),
                                          earnings_dates=store.load_us_earnings_calendar())}


@app.get("/api/gurus")
def gurus_get():
    """거장 포트폴리오(SEC 13F 스냅샷) + 보유종목에 우리 시그널 뱃지(S&P500 매칭분). 벤치마크 참고용."""
    gurus = store.load_gurus()
    idx = gurus_ref.build_name_index(store.load_us_universe())
    us_sig = _us_signals()
    for g in gurus:
        for h in g.get("holdings", []):
            tk = gurus_ref.match_ticker(h.get("name", ""), idx)
            h["ticker"] = tk
            sig = us_sig.get(tk) if tk else None
            # HOLD·시세없음은 뱃지 생략(요청: HOLD 제외)
            h["signal"] = {"kind": sig.kind, "score": round(sig.score, 2)} if (sig and sig.kind != "HOLD") else None
    return {"gurus": gurus}


_PEER_METRICS = [  # (key, 표시명, higher_is_better)
    ("per", "PER", False), ("pbr", "PBR", False), ("roe", "ROE(%)", True),
    ("revenue_growth", "매출성장(%)", True), ("debt_ratio", "부채비율(%)", False),
]


def _percentile_better(value: float, peers: list[float], higher_better: bool) -> float:
    """섹터 동종 대비 '이 값이 몇 %를 앞서나' — 0~100. 방향(높을수록/낮을수록 좋음)을 반영."""
    if not peers:
        return 50.0
    better = sum(1 for p in peers if (p <= value if higher_better else p >= value))
    return round(better / len(peers) * 100, 0)


@app.get("/api/signals/{ticker}/peers")
def signal_peers_get(ticker: str, market: str = "kospi"):
    """동종업계 비교(Koyfin식 percentile) — 선택 종목이 섹터 내에서 PER·PBR·ROE·성장·부채로 어디쯤인지 +
    같은 섹터 대표 종목들과 나란히. KR(재무 풍부)만 지원. 자문 아님 — 상대 위치 참고용."""
    if market == "us":
        return {"ready": False, "reason": "동종업계 비교는 현재 국내(재무 데이터 보유) 종목만 지원합니다."}
    fundamentals = store.load_fundamentals()
    sec = sectors.sector_of(ticker)
    me = fundamentals.get(ticker)
    if not sec or not me:
        return {"ready": False, "reason": "섹터·재무 데이터가 없어 비교할 수 없습니다."}
    peer_tks = [t for t in sectors.by_sector(sec) if t in fundamentals and t != ticker]
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    quotes = _quotes()
    sig_by = {s.ticker: s for s in _signals()} if store.is_ready() else {}
    metrics = []
    for key, label, hib in _PEER_METRICS:
        vals = [fundamentals[t][key] for t in peer_tks
                if isinstance(fundamentals[t].get(key), (int, float)) and fundamentals[t][key] > 0]
        mine = me.get(key)
        if not isinstance(mine, (int, float)) or (key != "revenue_growth" and mine <= 0):
            continue
        med = round(sorted(vals)[len(vals) // 2], 2) if vals else None
        metrics.append({"key": key, "label": label, "value": round(mine, 2), "median": med,
                        "better_pct": _percentile_better(mine, vals, hib), "higher_better": hib})
    # 같은 섹터 대표 종목(시총 상위 5) — 우리 시그널 뱃지 포함
    ranked = sorted(peer_tks, key=lambda t: (quotes.get(t) or {}).get("mktcap") or 0, reverse=True)[:5]
    peers = []
    for t in ranked:
        m, s = fundamentals[t], sig_by.get(t)
        peers.append({"ticker": t, "name": names.get(t, t), "per": m.get("per"), "pbr": m.get("pbr"),
                      "roe": m.get("roe"),
                      "signal": {"kind": s.kind, "score": round(s.score, 2)} if (s and s.kind != "HOLD") else None})
    return {"ready": True, "sector": sec, "peer_count": len(peer_tks), "metrics": metrics, "peers": peers}


@lru_cache(maxsize=1)
def _corp_codes():
    """DART stock_code→corp_code (zip 다운로드) — 요청마다 재다운로드 방지용 프로세스 캐시."""
    from signal_desk.ingest import dart
    return dart.corp_codes()


@lru_cache(maxsize=256)
def _disclosures_cached(corp_code: str, bgn: str, end: str) -> tuple:
    """공시 목록 캐시 — (report_nm, rcept_dt, rcept_no) 튜플. 키에 날짜 포함이라 매일 자연 무효화."""
    from signal_desk.ingest import dart
    return tuple((r["report_nm"], r["rcept_dt"], r["rcept_no"]) for r in dart.disclosures(corp_code, bgn, end))


def _disc_kind(nm: str) -> str:
    """공시명 → 이벤트 성격. good(호재)·caution(주의: 악재/희석/소송)·note(그 외 주목)."""
    if any(k in nm for k in kb._DISC_GOOD):
        return "good"
    if any(k in nm for k in (kb._DISC_CRITICAL + kb._DISC_SERIOUS)):
        return "caution"
    return "note"


@app.get("/api/signals/{ticker}/events")
def signal_events_get(ticker: str, market: str = "kospi"):
    """종목별 일정·이력 — KR: 최근 DART 주요공시(호재/주의, 과거) + 최근 연배당. US: 실적발표 예정일
    (Alpha Vantage 캘린더, 미래). 자문 아님, 맥락 참고용."""
    if market == "us":
        # 미국: 실적발표 예정일(미래) — AV 캘린더 캐시에서. 배당·공시는 KR만 지원.
        d = store.load_us_earnings_calendar().get(ticker)
        today = datetime.date.today().isoformat()
        upcoming = ([{"date": d, "label": "실적발표(예정)", "kind": "earnings"}]
                    if d and d >= today else [])
        return {"ready": True, "market": "us", "upcoming": upcoming, "disclosures": [], "dividend": None}
    corp = _corp_codes().get(ticker)
    disclosures = []
    if corp:
        end = datetime.date.today()
        bgn = end - datetime.timedelta(days=180)   # 최근 6개월 주요공시
        for nm, d, rno in _disclosures_cached(corp, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")):
            if not any(k in nm for k in kb._DISC_NOTABLE):   # 분기보고서·IR 등 routine 제외(노이즈)
                continue
            disclosures.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d, "name": nm,
                                "kind": _disc_kind(nm),
                                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"})
    f = store.load_fundamentals().get(ticker) or {}
    q = _quotes().get(ticker) or {}
    dps, price = f.get("dps"), q.get("price")
    dividend = ({"dps": round(dps, 1), "div_yield": round(dps / price * 100, 2) if (dps and price) else None}
                if dps and dps > 0 else None)
    return {"ready": True, "market": "kospi", "upcoming": [], "disclosures": disclosures[:20],
            "dividend": dividend, "has_corp": bool(corp)}


# ---------- 안내 에이전트(챗봇) — 도구 실행은 여기(실데이터 접근). 재분석 없이 READ만 ----------
_CHAT_KIND_KO = {"STRONG_BUY": "강력매수", "BUY": "매수", "HOLD": "관망", "SELL": "매도", "STRONG_SELL": "강력매도"}


def _chat_resolve_ticker(query: str) -> str | None:
    """종목명 또는 코드 → ticker(국내). 정확 코드 우선, 없으면 이름 부분일치."""
    q = (query or "").strip()
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    if q in names:
        return q
    cand = [t for t, n in names.items() if q and (q in n or n in q)]
    return cand[0] if cand else None


def _chat_signal_summary(ticker: str) -> dict | None:
    sig = next((s for s in _signals() if s.ticker == ticker), None) if store.is_ready() else None
    if not sig:
        return None
    q = _quotes().get(ticker) or {}
    f = store.load_fundamentals().get(ticker) or {}
    c = store.load_consensus_latest().get(ticker) or {}
    _fund = store.load_fundamentals()
    _sec = sectors.sector_of(ticker)
    _eff_med = target.sector_median_per(_fund, {t: sectors.sector_of(t) for t in _fund}).get(_sec) \
        or target.median_per(_fund)
    tg = target.compute(q.get("price"), f.get("per"), _eff_med,
                        store.load_price_series().get(ticker),
                        analyst_target=c.get("price_target_mean"), fwd_eps=c.get("fwd1_eps"))
    ups = [v for v in [(tg or {}).get("value_upside_pct"), (tg or {}).get("fwd_value_upside_pct"),
                       (tg or {}).get("analyst_upside_pct"), (tg or {}).get("resistance_upside_pct")]
           if isinstance(v, (int, float)) and v > 0]
    dg = db.kb_digest_get(ticker)
    return {"종목": sig.name, "코드": ticker, "섹터": sectors.sector_of(ticker),
            "시그널": _CHAT_KIND_KO.get(sig.kind, sig.kind), "종합점수": round(sig.score, 2),
            "신뢰도": sig.confidence, "팩터강약(-1~1)": sig.factor_scores,
            "근거": sig.reasons[:6], "PER": f.get("per"), "PBR": f.get("pbr"), "ROE": f.get("roe"),
            "현재가": q.get("price"), "등락%": q.get("change_pct"),
            "목표가상승여력%": round(max(ups), 1) if ups else None,
            "뉴스심리": (dg or {}).get("sentiment"), "뉴스요약": (dg or {}).get("summary"),
            "최근악재": sig.event_note if sig.event_risk else None}


def _make_chat_dispatch(uid: int, is_toss_owner: bool = False):
    """tool_name+input → JSON 문자열(실데이터). uid는 봇 포폴 조회용, is_toss_owner는 실계좌 조회 격리용."""
    def _j(obj):
        return json.dumps(obj, ensure_ascii=False, default=str)

    def dispatch(name: str, inp: dict) -> str:
        if name == "find_signal":
            t = _chat_resolve_ticker(inp.get("query", ""))
            if not t:
                return _j({"error": "해당 종목을 국내 유니버스에서 찾지 못함"})
            s = _chat_signal_summary(t)
            return _j(s or {"error": "시그널 데이터 없음"})
        if name == "list_signals":
            kind, lim = inp.get("kind", "all"), min(int(inp.get("limit", 10) or 10), 20)
            want = {"strong_buy": {"STRONG_BUY"}, "buy": {"STRONG_BUY", "BUY"}}.get(kind)
            rows = [s for s in _signals() if (want is None or s.kind in want)]
            rows = [s for s in rows if s.kind != "HOLD"] if kind == "all" else rows
            out = [{"종목": s.name, "코드": s.ticker, "시그널": _CHAT_KIND_KO.get(s.kind, s.kind),
                    "점수": round(s.score, 2), "섹터": sectors.sector_of(s.ticker)} for s in rows[:lim]]
            return _j({"개수": len(out), "목록": out})
        if name == "get_portfolio":
            st = bot.get_state(uid, "kr")
            return _j({"현금": st.get("cash"), "총평가": st.get("total_eval"), "총손익률%": st.get("pnl_pct"),
                       "보유": [{"종목": p.get("name"), "코드": p.get("ticker"), "수량": p.get("qty"),
                                "손익률%": p.get("last_pnl_pct")} for p in (st.get("positions") or [])]})
        if name == "get_events":
            t = _chat_resolve_ticker(inp.get("query", ""))
            return _j(signal_events_get(t) if t else {"error": "종목 못 찾음"})
        if name == "market_context":
            rg, mc = _regime(), _macro()
            bump = regime.buy_threshold_bump(rg, mc) if hasattr(regime, "buy_threshold_bump") else {}
            return _j({"국면": rg.get("regime"), "시장폭%": rg.get("breadth_pct"),
                       "평균모멘텀%": rg.get("avg_momentum_pct"), "거시요약": (mc or {}).get("narrative"),
                       "매수기준상향": bump})
        if name == "explain_term":
            term = (inp.get("term") or "").strip()
            for cat in glossary.CATEGORIES:
                for it in cat.get("items", []):
                    if term and (term in it["term"] or it["term"] in term):
                        return _j({"용어": it["term"], "쉬운설명": it["easy"], "왜보나": it.get("why"),
                                   "우리시그널": it.get("in_signal")})
            return _j({"error": f"'{term}' 용어 설명 없음 — 인사이트>학습 참고"})
        if name == "search_kb":
            kw = (inp.get("query") or "").strip()
            names = {u["ticker"]: u["name"] for u in store.load_universe()}
            docs = kb_search.retrieve(kw, k=6)   # 문서 단위 BM25 검색(뉴스·기고·영상 요약 원문)
            hits = [{"종목": names.get(d["ticker"], d["ticker"]), "코드": d["ticker"], "유형": d.get("doc_class"),
                     "제목": d.get("title"), "요약": d.get("summary")} for d in docs]
            return _j({"검색어": kw, "결과": hits or "관련 KB 문서 없음"})
        if name == "get_real_holdings":
            if not is_toss_owner:   # 격리: 계정 소유자 본인만
                return _j({"error": "실계좌(토스) 보유내역은 계정 소유자 본인만 조회할 수 있어요"})
            s = _toss_holdings_summary()
            return _j(s or {"error": "토스 실계좌 조회 실패(연동·자격증명 확인 필요)"})
        return _j({"error": f"알 수 없는 도구: {name}"})
    return dispatch


def _is_toss_owner(request: Request) -> bool:
    """요청자가 토스 실계좌 소유자(단일)인지. owner 미설정이면 항상 False(안전 기본)."""
    owner = config.toss_account_owner()
    if not owner:
        return False
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    return bool(u and (u.get("email") or "").lower() == owner)


def _toss_holdings_summary() -> dict | None:
    """토스 실보유 → 챗봇/요약용 압축(실제 원화값). owner-gated 호출부에서만 사용."""
    from signal_desk.ingest import toss
    res = toss.holdings(config.toss_account())
    if not res:
        return None
    items = [{"종목": it.get("name"), "코드": it.get("symbol"), "국가": it.get("marketCountry"),
              "수량": it.get("quantity"), "평단": it.get("averagePurchasePrice"), "현재가": it.get("lastPrice"),
              "손익률%": round(float((it.get("profitLoss") or {}).get("rate", 0)) * 100, 2)}
             for it in (res.get("items") or [])]
    pl = res.get("profitLoss") or {}
    return {"총평가_원": (res.get("marketValue") or {}).get("amount", {}).get("krw"),
            "총매입_원": (res.get("totalPurchaseAmount") or {}).get("krw"),
            "총손익률%": round(float(pl.get("rate", 0)) * 100, 2), "보유": items}


@app.post("/api/chat")
def chat_post(request: Request, data: dict = Body(...)):
    """안내 에이전트 — 이미 계산된 시그널·KB·포폴을 도구로 조회해 대화로 풀어준다(재분석·자문 없음)."""
    message = (data.get("message") or "").strip()
    if not message:
        return {"ok": False, "reply": "무엇이 궁금한지 적어 주세요."}
    history = data.get("history") or []   # [{role, content}] — 프런트가 최근 몇 턴만 전달
    return chat.answer(message, history=history[-8:],
                       dispatch=_make_chat_dispatch(_uid(request), _is_toss_owner(request)))


@app.post("/api/chat/stream")
def chat_stream(request: Request, data: dict = Body(...)):
    """안내 에이전트 — SSE 토큰 스트리밍. data: {"delta": "..."} 이벤트, 마지막에 [DONE]."""
    message = (data.get("message") or "").strip()
    history = (data.get("history") or [])[-8:]
    uid, owner = _uid(request), _is_toss_owner(request)

    def gen():
        if not message:
            yield "data: " + json.dumps({"delta": "무엇이 궁금한지 적어 주세요."}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        dispatch = _make_chat_dispatch(uid, owner)
        try:
            for kind, payload in chat.answer_stream(message, history=history, dispatch=dispatch):
                if kind == "text" and payload:
                    yield "data: " + json.dumps({"delta": payload}, ensure_ascii=False) + "\n\n"
        except Exception:
            yield "data: " + json.dumps({"delta": "\n(오류가 발생했어요.)"}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/meta")
def chat_meta_get():
    """챗봇 사용 가능 여부 + 페르소나 이름(프런트 초기화용)."""
    return {"available": chat.llm.available(), "name": chat.PERSONA_NAME}


@app.get("/api/my-holdings")
def my_holdings_get(request: Request):
    """토스 실계좌 보유내역 — 계정 소유자 '본인만'(owner-gated). 그 외 전원 403(데이터 경로 진입 불가).
    서버엔 토스 자격증명이 1개(소유자 계좌)뿐이라 반드시 owner로 격리한다."""
    if not _is_toss_owner(request):
        return JSONResponse({"ready": False, "forbidden": True,
                             "reason": "본인 계좌 소유자만 조회할 수 있습니다."}, status_code=403)
    from signal_desk.ingest import toss
    res = toss.holdings(config.toss_account())
    if res is None:
        return {"ready": False, "reason": "토스 자산 API 조회 실패 — 자격증명·계좌 연동을 확인하세요."}
    return {"ready": True, **res}


@app.post("/api/my-holdings/import")
def my_holdings_import(request: Request):
    """토스 실계좌 보유내역을 '내 보유종목'(수동 스토어)으로 가져와, 기존 섹터 히트맵·리밸런싱·시나리오
    기능이 실계좌 기준으로 돌게 한다. owner 본인만. 기존 수동 입력은 대체된다."""
    if not _is_toss_owner(request):
        return JSONResponse({"ok": False, "forbidden": True, "reason": "본인 계좌 소유자만 가능합니다."}, status_code=403)
    from signal_desk.ingest import toss
    res = toss.holdings(config.toss_account())
    if not res:
        return {"ok": False, "reason": "토스 조회 실패 — 자격증명·연동을 확인하세요."}
    uid = _uid(request)
    for h in db.holdings_list(uid):        # 실계좌로 대체(중복·잔여 제거)
        db.holdings_remove(uid, h["ticker"])
    n = 0
    for it in (res.get("items") or []):
        sym = (it.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            db.holdings_set(uid, sym, float(it.get("quantity") or 0), float(it.get("averagePurchasePrice") or 0))
            n += 1
        except (TypeError, ValueError):
            continue
    return {"ok": True, "imported": n}


@app.get("/api/guru-screens")
def guru_screens_get(market: str = "kospi"):
    """거장 전략 스크린 — 버핏·그레이엄·린치식 규칙으로 유니버스 필터(교육용 프리셋, 자문 아님).
    KR(재무 풍부)만 지원. 각 스크린별 통과 종목 + 우리 시그널·현재가 병합."""
    screens_meta = [{"key": s.key, "name": s.name, "style": s.style, "note": s.note,
                     "criteria": [c.label for c in s.criteria]} for s in guru_screens.SCREENS]
    if market == "us":
        return {"ready": False, "screens": screens_meta,
                "reason": "전략 스크린은 현재 국내(재무 데이터 보유) 종목만 지원합니다."}
    fundamentals = store.load_fundamentals()
    if not fundamentals:
        return {"ready": False, "screens": screens_meta, "reason": "재무 데이터가 아직 없습니다."}
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    quotes = _quotes()
    sig_by = {s.ticker: s for s in _signals()} if store.is_ready() else {}
    hits = guru_screens.run(fundamentals)
    results = []
    for meta in screens_meta:
        tks = hits.get(meta["key"], [])
        tks.sort(key=lambda t: (quotes.get(t) or {}).get("mktcap") or 0, reverse=True)
        items = []
        for t in tks[:12]:  # 시총 상위 일부만(과다 노출 방지)
            m, s = fundamentals[t], sig_by.get(t)
            items.append({"ticker": t, "name": names.get(t, t), "per": m.get("per"), "pbr": m.get("pbr"),
                          "roe": m.get("roe"), "revenue_growth": m.get("revenue_growth"),
                          "signal": {"kind": s.kind, "score": round(s.score, 2)} if (s and s.kind != "HOLD") else None})
        results.append({**meta, "count": len(tks), "tickers": tks, "items": items})  # tickers=전체 매칭(스크리너 프리셋 필터용)
    return {"ready": True, "screens": results}


@app.get("/api/etfs")
def etfs_get():
    """유명 ETF 구성종목 스냅샷(참고용) — 인사이트 탭 서클차트. 시그널·KB 무관."""
    return {"etfs": etfs_ref.all_etfs()}


@app.get("/api/brain")
def brain_get():
    """두뇌 레이어 엔진 헬스 스냅샷 — 파이프라인 노드 그래프 + 헬스 스코어 + 규칙 기반 findings.
    읽기 전용(제안까지, 자동 적용 X). 관리자 시각화·헬스체크용."""
    acc = {"ready": False}
    df = store.load_signal_history()
    if not df.empty:
        acc = accuracy.realized_accuracy(df.to_dict("records"), store.load_all_dated_closes())
    return brain.build(store.data_freshness(), acc, signalcfg.get_dict(), store.is_ready())


def _accuracy_snapshot() -> dict:
    """실측 accuracy dict — 제안 refresh·brain이 공유."""
    acc: dict = {"ready": False}
    df = store.load_signal_history()
    if not df.empty:
        acc = accuracy.realized_accuracy(df.to_dict("records"), store.load_all_dated_closes())
    return acc


@app.get("/api/brain/proposals")
def brain_proposals_list(status: str | None = "draft"):
    """두뇌 개선 제안 큐(관리자). status=draft|approved|rejected 또는 빈 값=전체.
    gate 요약(국면 적응 매수문턱)을 같이 내려 '시그널/봇 idle'과 트래커를 혼동하지 않게 한다.
    accuracy_summary는 카드 얕은 A/B(현재 정밀도·추정 IC)용."""
    st = (status or "").strip() or None
    if st == "all":
        st = None
    items = brain_proposals.list_proposals(status=st)
    _, adapt = signalcfg.effective_config(
        _regime() if store.is_ready() else None,
        _macro() if store.is_ready() else None,
        flow_result=store.load_market_flow() if store.is_ready() else None,
    )
    base = signalcfg.get_dict()
    acc = _accuracy_snapshot()
    cov = acc.get("coverage") or {}
    return {"items": items, "draft_count": db.brain_proposal_draft_count(),
            "history": signalcfg.history(limit=8),
            "accuracy_summary": {
                "ready": bool(acc.get("ready")),
                "buy_precision_pct": acc.get("buy_precision_pct"),
                "factor_ic": acc.get("factor_ic") or {},
                "matured_primary": cov.get("matured_primary"),
                "composite_ic": brain_proposals.composite_ic_estimate(
                    acc.get("factor_ic") or {}, base),
            },
            "gate": {
                "base_buy_threshold": base.get("buy_threshold"),
                "effective_buy_threshold": adapt.get("effective_buy_threshold"),
                "bump": adapt.get("bump") or 0.0,
                "reasons": list(adapt.get("reasons") or []),
                "regime_adaptive": bool((base.get("regime_adaptive") or 0) >= 0.5),
            }}


@app.post("/api/brain/proposals/refresh")
def brain_proposals_refresh():
    """실측 IC 기준으로 draft 제안 생성/갱신(자동 적용 없음)."""
    out = brain_proposals.refresh(_accuracy_snapshot(), signalcfg.get_dict())
    out["draft_count"] = db.brain_proposal_draft_count()
    return out


@app.post("/api/brain/proposals/{pid}/review")
def brain_proposals_review(pid: str, request: Request, data: dict = Body(default={})):
    """제안 승인|반려. 승인 시 patch→signalcfg + 이력(+승인 시점 accuracy), 시그널 캐시 무효화."""
    _admin_or_403(request)
    status = str(data.get("status") or "").strip()
    acc = _accuracy_snapshot() if status == "approved" else None
    out = brain_proposals.review(pid, status, str(data.get("note") or ""), accuracy=acc)
    if not out.get("ok"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=out.get("error") or "처리 실패")
    if status == "approved":
        _signals.cache_clear()
        _backtest.cache_clear()
        _backtest_analysis.cache_clear()
    return out


@app.get("/api/engine/config/history")
def engine_config_history(limit: int = 20):
    """엔진 설정 변경 이력(제안 승인·수동 적용 감사)."""
    return {"history": signalcfg.history(limit=min(int(limit or 20), 50))}


@app.get("/api/methods")
def methods_get():
    """퀀트 방법론 레퍼런스 카탈로그 — 두뇌 레이어(자가 진단)가 gap→검증방법 매핑에 참조.
    active(반영)/candidate(후보)/rejected(미채택)로 분류. 산식은 창작 아닌 업계 검증분만 등재."""
    return {"methods": quant_methods.all_methods(),
            "counts": {s: len(quant_methods.by_status(s)) for s in ("active", "candidate", "rejected")}}


@app.get("/api/macro")
def macro_get():
    """미 거시 시황(CPI·기준금리·10년물·나스닥·VIX) + 우호/비우호 요약 — FRED 기반.
    signals/macro.py 참고. FRED_API_KEY 없으면 ready=False."""
    data = _macro()
    if not data["indicators"]:
        # FRED 정량 지표는 없어도 미주은 시황 내러티브는 있을 수 있음(전광판 코멘터리)
        return {"ready": False, "indicators": [], "narrative": data.get("narrative")}
    return {"ready": True, **data}


# ---------- 시그널 엔진 설정(관리자) ----------
@app.get("/api/engine/config")
def engine_config_get():
    """팩터 가중치·임계값 + 현재 백테스트 적중률(price_based) — 관리자 파이프라인 뷰."""
    bt = _backtest() if store.is_ready() else {}
    wr = {r["kind"]: r for r in bt.get("by_signal", [])}
    return {"config": signalcfg.get_dict(), "winrate": wr, "method": bt.get("method")}


@app.post("/api/engine/config")
def engine_config_set(data: dict = Body(...)):
    """가중치·임계값 저장 → 시그널/백테스트 캐시 무효화(즉시 반영)."""
    before = signalcfg.get_dict()
    out = signalcfg.set_dict(data)
    signalcfg.append_history({
        "ts": int(time.time()), "source": "manual",
        "before": before, "after": out, "patch": {
            k: out[k] for k in out if before.get(k) != out.get(k)
        },
    })
    _signals.cache_clear()
    _backtest.cache_clear()
    _backtest_analysis.cache_clear()
    return {"ok": True, "config": out}


@app.post("/api/engine/reset")
def engine_config_reset():
    out = signalcfg.reset()
    _signals.cache_clear()
    _backtest.cache_clear()
    _backtest_analysis.cache_clear()
    return {"ok": True, "config": out}


# ---------- SPA 서빙 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")
