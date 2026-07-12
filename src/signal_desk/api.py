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

from signal_desk import auth, bot, chat, config, db, kb, kb_search, notify, shortform, signalcfg, store, strategy
from signal_desk.reference import (cycle, etfs as etfs_ref, glossary, guru_screens, gurus as gurus_ref,
                                    quant_methods, sectors, us_ko, valuechain)
from signal_desk.signals import accuracy, macro, narrative, opportunity, rebalance, regime, scenario, target, valuation
from signal_desk.signals.engine import (
    SignalConfig, _price_only_components, backtest_summary, combine,
    compute_indicator_series, evaluate, factor_contribution, signal_zones, walk_forward,
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
    """외부 KB 소스(미주은·오건영·유튜브) 하루 1회 자동수집 — 증분이라 새 글만 적재.
    best-effort(개별 실패 무시), fanding tt 만료 등은 조용히 스킵. 새 인사이트/시황 반영 위해 캐시 무효화."""
    if db.kv_get("kb_collect_date") == _kst_today():
        return
    got = False
    for fn in (kb.collect_fanding, kb.collect_outstanding, kb.collect_youtube):
        try:
            out = fn()
            got = got or bool(out.get("imported") or out.get("macro"))
        except Exception as e:
            log.warning("KB 자동수집 실패(%s): %s", getattr(fn, "__name__", "?"), type(e).__name__)
    if got:
        _signals.cache_clear()
        _macro.cache_clear()
    try:  # US 재무 백필 — EDGAR(순이익·자기자본, 무료·무제한) 위주 + AV(섹터 등) 소량. 여러 날 걸쳐 전량 채움
        us = [u["ticker"] for u in store.load_us_universe()]
        if us:
            store.fetch_us_fundamentals_edgar(us, max_calls=60)  # EDGAR companyfacts → PER/PBR
            store.fetch_us_fundamentals(us, max_calls=20)        # AV → shares/sector 보조
            _us_signals.cache_clear()
    except Exception as e:
        log.warning("US 재무 백필 실패: %s", type(e).__name__)
    db.kv_set("kb_collect_date", _kst_today())


def _refresh_live_quotes(open_markets: list[str]) -> None:
    """열린 시장 종목의 토스 현재가를 배치 조회해 store에 실시간가 오버레이 설정 → 시그널·현재가
    캐시 무효화. 봇 run_once는 store.load_price_series()를 읽으므로 자동으로 실시간가 기준이 된다.
    열린 시장 없거나 토스 미가용 시 오버레이 해제(종가 복귀). best-effort(실패 무시)."""
    from signal_desk.ingest import toss
    if not open_markets:
        store.clear_live_quotes(); store.note_live_attempt("closed")
        _signals.cache_clear(); _us_signals.cache_clear(); _quotes.cache_clear(); _regime.cache_clear()
        return
    if not toss.available():
        store.clear_live_quotes(); store.note_live_attempt("toss_off", open_markets)
        _signals.cache_clear(); _us_signals.cache_clear(); _quotes.cache_clear(); _regime.cache_clear()
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
        _signals.cache_clear(); _us_signals.cache_clear(); _quotes.cache_clear(); _regime.cache_clear()
    else:  # 토큰 실패 등으로 빈 응답 — 오버레이 유지 안 함, 시도 기록만
        store.note_live_attempt("no_quotes", open_markets)


async def _bot_loop():
    """자동매매봇 백그라운드 루프 — 봇을 켠 유저별로 순회. 시그널은 공용, 계좌는 paper(종가 기준).

    interval(기본 5분)마다 순회하되, 실제 체결은 각 시장 장중에만 한다 — KR은 KR장중(09:00~15:20 평일),
    US는 US장중(KST 근사 22:30~06:00). 장외엔 비현실적 종가 체결을 피하려고 자동매매를 건너뛴다.
    (수동 '지금 실행'은 장 시간과 무관하게 즉시 실행 — 테스트용 override.)
    KB 자동수집·종가 스냅샷은 하루 1회(kv 날짜 가드)."""
    interval = config.bot_run_interval_minutes() * 60
    while True:
        try:
            _daily_kb_collect()  # 외부 소스(미주은·오건영·유튜브) 하루 1회 자동수집(공용)
            enabled = db.user_bots_enabled()
            open_markets = [m for m, is_open in
                            (("kr", bot.is_market_hours()), ("us", bot.is_us_market_hours())) if is_open]
            _refresh_live_quotes(open_markets)  # 장중 실시간가 오버레이 갱신(열린 시장만, 없으면 종가 복귀)
            for uid in enabled:  # 장중인 시장만 체결(장외 스킵)
                for mkt in open_markets:
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
                for uid in enabled:
                    bot.snapshot_positions(uid, "kr")
                    bot.snapshot_positions(uid, "us")
                db.kv_set("bot_daily_snap", _kst_today())
        except Exception as e:
            log.error("자동매매봇 루프 오류: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        bot.ensure_reference_bots()  # 공용 레퍼런스 봇(성향별) 부트스트랩 — 루프가 자동 운용
    except Exception as e:
        log.warning("레퍼런스 봇 부트스트랩 실패: %s", type(e).__name__)
    task = asyncio.create_task(_bot_loop())
    yield
    task.cancel()


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
    "/api/refresh", "/api/engine/config", "/api/engine/reset", "/api/backtest/analysis",
    "/api/kb/refresh", "/api/kb/import", "/api/kb/import-file", "/api/kb/documents", "/api/kb/digests",
    "/api/kb/collect-fanding", "/api/kb/collect-outstanding", "/api/kb/collect-youtube",
    "/api/shortform/generate", "/api/shortform/generate-performance",
    "/api/shortform/queue", "/api/shortform/candidates",
    "/api/data-health", "/api/egress-ip",
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


def _us_signal_items() -> list[dict]:
    """미국(S&P500) 시그널 항목 — KOSPI와 동일 형태. 재무·KB·밸류체인 없어 관련 필드는 null,
    섹터는 GICS(us_universe)에서. 현재가·등락은 us_prices 마지막 두 종가로."""
    sig = _us_signals()
    if not sig:
        return []
    sector_of = {u["ticker"]: u.get("sector") for u in store.load_us_universe()}
    hist = store.load_us_price_series()
    quotes = store.load_us_quotes()  # 거래량·20일평균(정렬용)
    mcaps = store.us_marketcaps(hist)  # 시총(주식수×종가)·PER(Alpha Vantage 백필분, 없으면 빈 dict)
    us_pers = sorted(mc["per"] for mc in mcaps.values() if mc.get("per") and mc["per"] > 0)
    us_med_per = us_pers[len(us_pers) // 2] if us_pers else None  # US 밸류 정상화 기준(가용 PER 중앙값)
    items = []
    for r in sig.values():
        closes = hist.get(r.ticker) or []
        price = closes[-1] if closes else None
        prev = closes[-2] if len(closes) >= 2 else None
        q = quotes.get(r.ticker) or {}
        d = asdict(r)
        d["name"] = us_ko.name_ko(r.ticker, r.name)   # 한글명(주요 종목) + 티커
        d["price"] = price
        d["change_pct"] = round((price / prev - 1) * 100, 2) if (price and prev) else None
        d["vol"] = q.get("vol")
        d["vol_avg"] = q.get("vol_avg")               # 거래량순 정렬 반영
        mc = mcaps.get(r.ticker) or {}
        d["mktcap"] = mc.get("mktcap")                # 시총순 정렬(백필된 종목만)
        d["per"] = mc.get("per")                       # US PER(EDGAR 순이익, 없으면 AV)
        d["pbr"] = mc.get("pbr")                        # US PBR(EDGAR 자기자본)
        d["sector"] = us_ko.sector_ko(sector_of.get(r.ticker))  # 한글 섹터
        d["intro"] = f"{d['sector']} 섹터" if d["sector"] else None  # US는 밸류체인 매핑 없음 → 섹터로 대체
        d["intro_desc"] = None
        d["kb"] = None
        d["target"] = target.compute(price, mc.get("per"), us_med_per, closes)  # 참고 목표가(저항선 + 가능시 밸류)
        d["opp_tags"] = opportunity.classify(r)  # 기회 유형(#14)
        items.append(d)
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


@app.get("/api/signals")
def signals_get(market: str = "kospi"):
    if market == "us":
        items = _us_signal_items()
        if not items:
            return {"ready": False, "items": [], "message": "미국 종목 시세가 아직 없습니다 — 백필 후 표시됩니다."}
        return {"ready": True, "items": items}
    if not store.is_ready():
        return {"ready": False, "items": [], "message": "아직 수집된 데이터가 없습니다. /api/refresh를 먼저 호출하세요."}
    items = []
    quotes = _quotes()
    fundamentals = store.load_fundamentals()
    med_per = target.median_per(fundamentals)   # 목표가(밸류 정상화) 기준 — 루프 밖 1회
    sec_med_per = target.sector_median_per(fundamentals, {t: sectors.sector_of(t) for t in fundamentals})  # 섹터중립 v1
    price_series = store.load_price_series()      # 기술적 저항 산정용
    consensus = store.load_consensus_latest()     # 목표가 v2 앵커(선행EPS·애널 목표주가, KR)
    for r in _signals():
        d = asdict(r)
        q = quotes.get(r.ticker) or {}
        d["price"] = q.get("price")  # 현재가(최신 종가)
        d["change_pct"] = q.get("change_pct")
        d["mktcap"] = q.get("mktcap")  # 시가총액(정렬·표기용)
        d["vol"] = q.get("vol")
        d["vol_avg"] = q.get("vol_avg")  # 최근 20일 평균 거래량
        f = fundamentals.get(r.ticker) or {}  # 저평가 팩터 근거(PER/PBR) — 탭 대신 시그널 상세에 표시
        d["per"] = f.get("per")
        d["pbr"] = f.get("pbr")
        d["roe"] = f.get("roe")                    # 스크리너 필터용(재무 지표)
        d["debt_ratio"] = f.get("debt_ratio")
        d["revenue_growth"] = f.get("revenue_growth")
        dps, px = f.get("dps"), d.get("price")
        d["div_yield"] = round(dps / px * 100, 2) if (dps and px) else None  # 배당수익률(%)
        pos = valuechain.company_position(r.ticker)  # 밸류체인 큐레이션에서 소개 재활용
        d["sector"] = sectors.sector_of(r.ticker)  # 세분 섹터(조선·철강·화장품·로봇 등) 200종목 매핑
        d["intro"] = f"{pos['sector']} 밸류체인 · {pos['stage']}" if pos else None
        d["intro_desc"] = pos["stage_desc"] if pos else None
        dg = db.kb_digest_get(r.ticker)  # KB 정성 다이제스트(뉴스·영상 가공)
        d["kb"] = {"sentiment": dg["sentiment"], "summary": dg["summary"], "points": dg["points"]} if dg else None
        d["opp_tags"] = opportunity.classify(r)  # 기회 유형(#14)
        c = consensus.get(r.ticker) or {}
        eff_med_per = sec_med_per.get(d["sector"]) or med_per  # 섹터 중앙값 PER(없으면 유니버스 fallback)
        d["target"] = target.compute(d["price"], f.get("per"), eff_med_per, price_series.get(r.ticker),
                                     analyst_target=c.get("price_target_mean"), fwd_eps=c.get("fwd1_eps"))  # 참고 목표가(v2+섹터중립)
        items.append(d)
    return {"ready": True, "items": items}


@app.get("/api/narrative")
def narrative_get(ticker: str):
    """시그널 해설 v2(#17) — 근거+KB를 LLM으로 해설(캐시). LLM 미설정/실패 시 규칙기반 v1 폴백."""
    sig = next((s for s in _signals() if s.ticker == ticker), None) if store.is_ready() else None
    if sig is None:
        sig = _us_signals().get(ticker)
    if sig is None:
        return {"ok": False, "reason": "해당 종목 시그널이 없습니다."}
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    names.update({u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in store.load_us_universe()})
    name = names.get(ticker, sig.name)
    dg = db.kb_digest_get(ticker)
    kb_summary = (dg or {}).get("summary") or ""
    # 데이터 스냅샷 해시로 캐시 키 — 시그널/KB가 바뀌면 자동 무효화
    h = hashlib.md5(f"{sig.kind}|{round(sig.score, 1)}|{kb_summary}".encode()).hexdigest()[:12]
    key = f"narrv2:{ticker}:{h}"
    cached = db.kv_get(key)
    if cached:
        return {"ok": True, "narrative": cached, "source": "llm", "cached": True}
    text = narrative.explain_llm(name, ticker, sig.kind, sig.score, sig.reasons, kb_summary)
    if text:
        db.kv_set(key, text)
        return {"ok": True, "narrative": text, "source": "llm", "cached": False}
    return {"ok": True, "narrative": sig.narrative, "source": "rule", "cached": False}  # v1 폴백


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


@app.get("/api/signals/{ticker}/chart")
def signal_chart_get(ticker: str, market: str = "kospi"):
    """종목 가격+지표 시계열(차트용) — 종가/MA20·60·120/RSI/MACD. market=us면 미국 시세."""
    history = store.load_us_price_history(ticker) if market == "us" else store.load_price_history(ticker)
    if not history:
        return {"ready": False, "dates": []}
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
    stored = store.signal_history_for(ticker) if market != "us" else {}  # 실측 시그널(PIT) 우선
    actual_dates = [d for d in dates if d in stored]
    return {
        "ready": True,
        "ticker": ticker,
        "quote": None if market == "us" else _quotes().get(ticker),  # US는 헤더 quote 별도 없음(현재가는 항목에)
        "dates": dates,
        "close": closes,
        "ma20": series["ma_short"],
        "ma60": series["ma_mid"],
        "ma120": series["ma_long"],
        "rsi": series["rsi"],
        "zones": signal_zones(dates, closes, stored=stored),
        "actual_from": actual_dates[0] if actual_dates else None,  # 이 날짜 이후는 실측(그 전은 재현)
        "macd": series["macd"]["macd"],
        "macd_signal": series["macd"]["signal"],
        "macd_hist": series["macd"]["histogram"],
    }


@app.get("/api/market/chart")
def market_chart_get():
    """코스피200 근사 지수 차트 — 시그널 탭 최상단 고정. 종목 차트와 동일하게 MA/RSI/MACD +
    매수/매도 구간 + 현재 시그널(가격기반)을 함께 준다."""
    history = store.load_index_history()
    if not history:
        return {"ready": False, "dates": []}
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
    cfg = SignalConfig()
    combined = combine(_price_only_components(closes, series, len(closes) - 1, cfg), cfg)
    return {
        "ready": True, "ticker": "KOSPI200X", "name": "코스피200 지수(근사)",
        "dates": dates, "close": closes,
        "ma20": series["ma_short"], "ma60": series["ma_mid"], "ma120": series["ma_long"],
        "rsi": series["rsi"], "zones": signal_zones(dates, closes),
        "macd": series["macd"]["macd"], "macd_signal": series["macd"]["signal"], "macd_hist": series["macd"]["histogram"],
        "kind": combined["kind"], "score": combined["score"], "confidence": combined["confidence"],
        "reasons": combined["reasons"],
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
    _us_signals.cache_clear()


def _refresh_kr(data: dict) -> dict:
    """국내 유니버스+시세+재무(+PER/PBR·퀄리티·배당). DART 재무는 분기(≈80일)마다만 재수집하고
    (연간 데이터라 거의 불변), 그 외엔 시총만 다시 받아 매일 재계산. force_dart=true면 강제."""
    universe = store.fetch_universe()
    store.fetch_prices(universe)
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
    return {"universe_size": len(universe), "fundamentals_size": len(fundamentals)}


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


def _refresh_us(data: dict) -> dict:
    """미국: 거장 13F + S&P500 유니버스/발행주식수/EDGAR 재무(증분 백필) + 거장 보유종목 시세."""
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
        idx = gurus_ref.build_name_index(us_uni)  # 거장 보유종목 → 시세 수집(뱃지용, 스로틀)
        us_tks = sorted({t for g in store.load_gurus() for h in g.get("holdings", [])
                         if (t := gurus_ref.match_ticker(h.get("name", ""), idx))})
        if us_tks:
            store.fetch_us_prices(us_tks)
    except Exception as e:
        log.warning("거장/US 수집 실패(무시): %s", e)
    us_fund = store.load_us_fundamentals()
    us_filled = sum(1 for f in us_fund.values() if f.get("net_income") is not None or f.get("equity") is not None)
    return {"us_fund_filled": us_filled, "us_universe_size": len(us_fund) or None}


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
def _kb_targets(limit_candidates: int = 12) -> list[dict]:
    """KB 갱신 대상 — 보유종목 + 상위 BUY 후보 + 관심종목. 리소스 절약 위해 전 종목 아님."""
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    targets, seen = [], set()
    def add(ticker):
        if ticker in names and ticker not in seen:
            targets.append({"ticker": ticker, "name": names[ticker]}); seen.add(ticker)
    if store.is_ready():
        for s in _signals():
            if s.kind == "BUY" and len([t for t in targets]) < limit_candidates:
                add(s.ticker)
    for tk in db.bot_position_tickers_all():  # 전 유저 보유 종목(공용 KB 갱신 대상)
        add(tk)
    return targets


@app.post("/api/kb/refresh")
def kb_refresh():
    """뉴스·영상 수집 → LLM 다이제스트 → KB 적재(대상: 보유+상위 BUY 후보). 시그널 캐시 무효화."""
    targets = _kb_targets()
    if not targets:
        return {"ok": False, "reason": "대상 종목 없음 — /api/refresh로 유니버스 먼저 수집"}
    out = kb.refresh(targets)
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
@app.get("/api/cycle")
def cycle_get():
    """경기 사이클 4국면 + 국면별 주도섹터, 현재 위치(FRED 거시로 근사 추정).
    각 주도섹터에 밸류체인 섹터 key(vc_key)를 달아 밸류체인 탭과 연결한다."""
    phases = []
    for p in cycle.phases():
        leads = [{"name": s, "vc_key": valuechain.key_for_tag(s)} for s in p["lead_sectors"]]
        phases.append({**p, "lead_sectors": leads})
    return {"phases": phases, "current": cycle.position(_macro()["indicators"])}


@app.get("/api/glossary")
def glossary_get():
    """투자 용어·지표 학습 사전(스터디) — 카테고리별 개념/쉬운설명/왜보는지/우리시그널에서."""
    return {"categories": glossary.categories()}


@app.get("/api/valuechain")
def valuechain_get():
    """섹터별 밸류체인(업→다운스트림) 대표기업 큐레이션. 국내는 티커로 시그널 연결 가능.
    현재 경기국면(cycle)에 유리한 밸류체인을 cycle_fit로 태깅 — 사이클×밸류체인×시그널 내러티브."""
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
        "lead_sectors": pos.get("lead_sectors") or [], "reasons": pos.get("reasons") or []}}


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
    out = signalcfg.set_dict(data)
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
