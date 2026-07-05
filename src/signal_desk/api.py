"""FastAPI 백엔드 — 인증/온보딩/워치리스트, 시그널/밸류에이션/국면 실데이터, SPA 서빙.

1단계 스캐폴딩 범위였던 스텁 라우트 중 후보(candidates)/매크로/AI리포트는 아직 스키마만
확정한 스텁으로 남아 있고(phase3~6), 실제 계산 로직은 signals/, ingest/에서 채워 나간다.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, Request
from fastapi import File as FastFile
from fastapi import Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from signal_desk import auth, bot, config, db, kb, signalcfg, store, strategy
from signal_desk.reference import cycle, gurus as gurus_ref, sectors, us_ko, valuechain
from signal_desk.signals import macro, rebalance, regime, scenario, valuation
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


async def _bot_loop():
    """자동매매봇 백그라운드 루프. enabled=False면 조용히 skip(기본 OFF).

    장중(5분 주기): run_once로 매매. 마감 후 1회: 다음날 예약 생성. 개장 직후 1회: 예약 실행.
    하루 1회성 작업은 kv에 마지막 실행일을 기록해 중복 방지."""
    interval = config.bot_run_interval_minutes() * 60
    while True:
        try:
            if db.bot_config_get()["enabled"]:
                now = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
                weekday = now.weekday() < 5
                if bot.is_market_hours(now):
                    # 개장 직후(09:00~09:10) 예약 먼저 실행 후, 평상시 매매
                    if now.time() <= datetime.time(9, 10) and db.kv_get("bot_exec_resv_date") != _kst_today():
                        bot.execute_reservations()
                        db.kv_set("bot_exec_resv_date", _kst_today())
                    result = bot.run_once()
                    if not result.get("ok"):
                        log.info("자동매매봇 실행 스킵: %s", result.get("reason"))
                elif weekday and now.time() >= datetime.time(15, 40) and db.kv_get("bot_resv_date") != _kst_today():
                    # 마감 후 1회: KB(뉴스) 갱신 → 신선한 정성/이벤트 반영 후 다음 개장용 예약 생성
                    try:
                        kb.refresh(_kb_targets())
                        _signals.cache_clear()
                    except Exception as e:
                        log.warning("마감후 KB 갱신 실패(예약은 계속): %s", e)
                    bot.snapshot_positions()  # 종가 기준 보유종목 현재가·수익률 1회 갱신
                    bot.generate_reservations()
                    db.kv_set("bot_resv_date", _kst_today())
        except Exception as e:
            log.error("자동매매봇 루프 오류: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
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
    "/api/kb/refresh", "/api/kb/import", "/api/kb/import-file", "/api/kb/documents",
    "/api/kb/collect-fanding", "/api/kb/collect-outstanding", "/api/kb/collect-youtube",
}


# ---------- 인증 ----------
@app.post("/api/auth/signup")
def auth_signup(data: dict = Body(...)):
    token, err = auth.signup(data.get("email", ""), data.get("pw", ""))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return r


@app.post("/api/auth/login")
def auth_login(data: dict = Body(...)):
    token, err = auth.login(data.get("email", ""), data.get("pw", ""))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
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


@app.post("/api/rebalance")
def rebalance_post(request: Request, data: dict = Body(default={})):
    """내 보유종목(국내+해외 혼합)을 시그널·성향 목표배분에 맞춰 리밸런싱 제안 + LLM 해설.
    성향은 요청에서 받는다(기본 균형형)."""
    holdings = db.holdings_list(_uid(request))
    if not holdings:
        return {"ready": False, "reason": "보유종목을 먼저 입력하세요."}
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
    """내 보유종목을 부트스트랩 몬테카를로로 전략별 N년 후 가치 분포로 투영(#9)."""
    holdings = db.holdings_list(_uid(request))
    if not holdings:
        return {"ready": False, "reason": "보유종목을 먼저 입력하세요."}
    if not store.is_ready():
        return {"ready": False, "reason": "시세 데이터가 없습니다 — /api/refresh 먼저."}
    prices = {**store.load_price_series(), **store.load_us_price_series()}
    years = min(max(int(data.get("years", 3)), 1), 10)
    return scenario.project(holdings, prices, years=years)


@app.get("/api/portfolio/heatmap")
def portfolio_heatmap(request: Request):
    """내 보유종목을 섹터별로 묶은 히트맵(#12) — 평가액 크기 + 손익률 색상. 국내·해외 혼합."""
    holdings = db.holdings_list(_uid(request))
    if not holdings:
        return {"ready": False, "reason": "보유종목을 먼저 입력하세요."}
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
    cfg, _ = signalcfg.effective_config(_regime(), _macro())  # 약세·비우호 국면이면 매수 기준 자동 상향
    return evaluate(store.load_universe(), store.load_price_series(), store.load_fundamentals(),
                    config=cfg, sentiment=kb.sentiment_map())


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
    indicators = store.load_macro()
    # 정량 지표(FRED) + 정성 내러티브(미주은 시황 코멘터리 — 시장흐름 트래킹용, 개별 종목엔 미반영)
    return {"indicators": indicators, "narrative": kb.macro_digest(), **macro.read(indicators)}


def _us_signal_items() -> list[dict]:
    """미국(S&P500) 시그널 항목 — KOSPI와 동일 형태. 재무·KB·밸류체인 없어 관련 필드는 null,
    섹터는 GICS(us_universe)에서. 현재가·등락은 us_prices 마지막 두 종가로."""
    sig = _us_signals()
    if not sig:
        return []
    sector_of = {u["ticker"]: u.get("sector") for u in store.load_us_universe()}
    hist = store.load_us_price_series()
    quotes = store.load_us_quotes()  # 거래량·20일평균(정렬용) — 시총은 소스 없어 미제공
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
        d["mktcap"] = d["per"] = d["pbr"] = None       # US 시총·재무는 미수집
        d["sector"] = us_ko.sector_ko(sector_of.get(r.ticker))  # 한글 섹터
        d["intro"] = d["intro_desc"] = d["kb"] = None
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
        pos = valuechain.company_position(r.ticker)  # 밸류체인 큐레이션에서 소개 재활용
        d["sector"] = sectors.sector_of(r.ticker)  # 세분 섹터(조선·철강·화장품·로봇 등) 200종목 매핑
        d["intro"] = f"{pos['sector']} 밸류체인 · {pos['stage']}" if pos else None
        d["intro_desc"] = pos["stage_desc"] if pos else None
        dg = db.kb_digest_get(r.ticker)  # KB 정성 다이제스트(뉴스·영상 가공)
        d["kb"] = {"sentiment": dg["sentiment"], "summary": dg["summary"], "points": dg["points"]} if dg else None
        items.append(d)
    return {"ready": True, "items": items}


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


@app.get("/api/signals/{ticker}/chart")
def signal_chart_get(ticker: str, market: str = "kospi"):
    """종목 가격+지표 시계열(차트용) — 종가/MA20·60·120/RSI/MACD. market=us면 미국 시세."""
    history = store.load_us_price_history(ticker) if market == "us" else store.load_price_history(ticker)
    if not history:
        return {"ready": False, "dates": []}
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
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
        "zones": signal_zones(dates, closes),
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


@app.post("/api/refresh")
def refresh():
    """유니버스+시세(+DART 키 있으면 재무)를 재수집하고 시그널/백테스트/밸류에이션 캐시를 무효화."""
    universe = store.fetch_universe()
    store.fetch_prices(universe)
    fundamentals = store.fetch_fundamentals(universe)
    store.fetch_fundamentals_history(universe)  # point-in-time 백테스트용 연도별 재무
    macro_items = store.fetch_macro()
    try:
        store.fetch_gurus()  # 거장 포트폴리오(SEC 13F) — 실패해도 나머지 수집엔 영향 없음
        us_uni = store.fetch_us_universe()  # S&P500 유니버스
        idx = gurus_ref.build_name_index(us_uni)  # 거장 보유종목 → 시세 수집(뱃지용, 스로틀)
        us_tks = sorted({t for g in store.load_gurus() for h in g.get("holdings", [])
                         if (t := gurus_ref.match_ticker(h.get("name", ""), idx))})
        if us_tks:
            store.fetch_us_prices(us_tks)
    except Exception as e:
        log.warning("거장/US 수집 실패(무시): %s", e)
    _signals.cache_clear()
    _backtest.cache_clear()
    _backtest_analysis.cache_clear()
    _valuation.cache_clear()
    _quotes.cache_clear()
    _regime.cache_clear()
    _macro.cache_clear()
    _us_signals.cache_clear()
    return {
        "ok": True,
        "universe_size": len(universe),
        "fundamentals_size": len(fundamentals),
        "macro_size": len(macro_items),
    }


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
    _, adapt = signalcfg.effective_config(_regime(), _macro())  # 국면 적응으로 상향된 매수 기준
    return {**_regime(), "adaptive": adapt}


# ---------- 자동매매봇 (BACKLOG #7, KIS 모의투자) ----------
@app.get("/api/bot/state")
def bot_state_get():
    return bot.get_state()


@app.post("/api/bot/toggle")
def bot_toggle(data: dict = Body(...)):
    bot.set_enabled(bool(data.get("enabled")))
    return {"ok": True, "enabled": bool(data.get("enabled"))}


@app.post("/api/bot/style")
def bot_style(data: dict = Body(...)):
    """트레이딩 성향(안정형/균형형/공격형) 변경 — 봇 파라미터·리스크 룰이 프리셋으로 바뀐다."""
    style = bot.set_style(str(data.get("style", "balanced")))
    return {"ok": True, "style": style}


@app.post("/api/bot/run")
def bot_run():
    """수동 1회 실행 — 실주문은 장 시간(평일 09:00~15:20 KST)에만 나간다."""
    return bot.run_once(dry_run=False)


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
def bot_preview():
    """판단 미리보기(dry-run) — 주문 없이 '지금 무엇을 왜 매매할지' 계획만 계산. 장 시간 무관."""
    return bot.run_once(dry_run=True)


@app.post("/api/bot/reset")
def bot_reset():
    """봇 포지션·거래내역 초기화(설정 유지) — 과거 유령거래 등 정합성 깨진 상태 정리용."""
    db.bot_reset()
    return {"ok": True}


@app.post("/api/bot/reserve")
def bot_reserve(data: dict = Body(default={})):
    """마감 후 예약 주문 생성(수동 트리거). dry_run이면 계획만."""
    return bot.generate_reservations(dry_run=bool(data.get("dry_run")))


@app.post("/api/bot/execute-reservations")
def bot_execute_reservations(data: dict = Body(default={})):
    """대기 중인 예약을 지금 실행(수동 트리거). dry_run이면 계획만."""
    return bot.execute_reservations(dry_run=bool(data.get("dry_run")))


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
    for p in db.bot_positions_all():
        add(p["ticker"])
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
    """fanding.kr 미주은 최신 포스트 → 종목 특정분만 전문가 인사이트 KB로 적재(수동 트리거)."""
    limit = int(data.get("limit", 15))
    out = kb.collect_fanding(limit=limit, force=bool(data.get("force")))
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
    """유튜브 화이트리스트 채널 최신 영상(자막 전문) → 거시 KB(상장사 특정 영상은 종목 KB) 적재."""
    n = int(data.get("max_per_channel", 8))
    out = kb.collect_youtube(max_per_channel=n, force=bool(data.get("force")))
    if out.get("ok") and out.get("imported"):
        _signals.cache_clear()
    if out.get("ok") and out.get("macro"):
        _macro.cache_clear()
    return out


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
async def kb_import_file(ticker: str = Form(...), file: UploadFile = FastFile(...)):
    """PDF·이미지 업로드 → (텍스트 PDF는 pypdf, 스캔·이미지는 모델 OCR) 요약·분류 후 KB 적재."""
    names = {u["ticker"]: u["name"] for u in store.load_universe()}
    name = names.get(ticker.strip())
    if not name:
        return {"ok": False, "reason": "유니버스에 없는 종목코드입니다(ticker 확인)"}
    media_type = file.content_type or ""
    if media_type not in _UPLOAD_TYPES:
        return {"ok": False, "reason": f"지원 형식 아님({media_type}) — PDF·PNG·JPG만"}
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        return {"ok": False, "reason": "파일이 너무 큽니다(최대 15MB)"}
    out = kb.import_file(ticker.strip(), name, file.filename or "", data, media_type)
    if out.get("ok"):
        _signals.cache_clear()
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


@app.get("/api/valuechain")
def valuechain_get():
    """섹터별 밸류체인(업→다운스트림) 대표기업 큐레이션. 국내는 티커로 시그널 연결 가능."""
    return {"sectors": valuechain.sectors()}


@lru_cache(maxsize=1)
def _us_signals():
    """미국 종목 시그널 — US 유니버스 중 시세 있는 종목만(재무 없음 → 저평가 팩터 자동 제외).
    KB 감성(미주은 등 전문가 인사이트)은 정성 팩터로 반영. 반환: {ticker: SignalResult}."""
    prices = store.load_us_price_series()
    if not prices:
        return {}
    return {s.ticker: s for s in evaluate(store.load_us_universe(), prices, sentiment=kb.sentiment_map())}


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
