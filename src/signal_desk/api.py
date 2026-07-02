"""FastAPI 백엔드 — 인증/온보딩/워치리스트, 시그널/밸류에이션/국면 실데이터, SPA 서빙.

1단계 스캐폴딩 범위였던 스텁 라우트 중 후보(candidates)/매크로/AI리포트는 아직 스키마만
확정한 스텁으로 남아 있고(phase3~6), 실제 계산 로직은 signals/, ingest/에서 채워 나간다.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from signal_desk import auth, bot, config, db, store
from signal_desk.reference import cycle, valuechain
from signal_desk.signals import macro, regime, valuation
from signal_desk.signals.engine import backtest_summary, compute_indicator_series, evaluate, signal_zones

config.load_env()

log = logging.getLogger("signal_desk")

WEB_DIR = Path(__file__).parent / "web"

# 인증 게이트: /api/* 는 세션 필수(아래 prefix 만 예외). 그 외(/, 정적)는 허용.
_OPEN_PREFIXES = ("/api/auth/",)


def _uid(request: Request):
    u = auth.current_user(request.cookies.get(auth.COOKIE))
    return u["id"] if u else None


async def _bot_loop():
    """자동매매봇 백그라운드 루프 — apt-signal의 자동갱신 루프와 같은 패턴(최초 도입).
    bot_config.enabled가 False면 조용히 skip(기본값 OFF — 사용자가 명시적으로 켜야 실행)."""
    interval = config.bot_run_interval_minutes() * 60
    while True:
        try:
            if db.bot_config_get()["enabled"]:
                result = bot.run_once()
                if not result.get("ok"):
                    log.info("자동매매봇 실행 스킵: %s", result.get("reason"))
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
    return await call_next(request)


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
    return {"auth": True, "email": u["email"], "profile": profile, "onboarded": bool(profile)}


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


# ---------- 시그널 (실데이터, store 캐시 기반) ----------
@lru_cache(maxsize=1)
def _signals():
    return evaluate(store.load_universe(), store.load_price_series(), store.load_fundamentals())


@lru_cache(maxsize=1)
def _backtest():
    return backtest_summary(store.load_price_series())


@lru_cache(maxsize=1)
def _valuation():
    return valuation.screen(store.load_universe(), store.load_fundamentals())


@lru_cache(maxsize=1)
def _regime():
    return regime.classify(store.load_price_series())


@lru_cache(maxsize=1)
def _macro():
    indicators = store.load_macro()
    return {"indicators": indicators, **macro.read(indicators)}


@app.get("/api/signals")
def signals_get():
    if not store.is_ready():
        return {"ready": False, "items": [], "message": "아직 수집된 데이터가 없습니다. /api/refresh를 먼저 호출하세요."}
    items = []
    for r in _signals():
        d = asdict(r)
        pos = valuechain.company_position(r.ticker)  # 밸류체인 큐레이션에서 섹터·소개 재활용
        d["sector"] = pos["gics"] if pos else None  # 시그널 리스트 컬럼은 GICS 섹터
        d["intro"] = f"{pos['sector']} 밸류체인 · {pos['stage']}" if pos else None
        d["intro_desc"] = pos["stage_desc"] if pos else None
        items.append(d)
    return {"ready": True, "items": items}


@app.get("/api/backtest")
def backtest_get():
    """시그널 적중률 성적표 — 1차 버전은 기술점수 단독(engine.backtest_summary 참고)."""
    if not store.is_ready():
        return {"ready": False}
    return {"ready": True, **_backtest()}


@app.get("/api/signals/{ticker}/chart")
def signal_chart_get(ticker: str):
    """종목 가격+지표 시계열(차트용) — 종가/MA20·60·120/RSI/MACD."""
    history = store.load_price_history(ticker)
    if not history:
        return {"ready": False, "dates": []}
    closes = [h["close"] for h in history]
    dates = [h["date"] for h in history]
    series = compute_indicator_series(closes)
    return {
        "ready": True,
        "ticker": ticker,
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


@app.post("/api/refresh")
def refresh():
    """유니버스+시세(+DART 키 있으면 재무)를 재수집하고 시그널/백테스트/밸류에이션 캐시를 무효화."""
    universe = store.fetch_universe()
    store.fetch_prices(universe)
    fundamentals = store.fetch_fundamentals(universe)
    macro_items = store.fetch_macro()
    _signals.cache_clear()
    _backtest.cache_clear()
    _valuation.cache_clear()
    _regime.cache_clear()
    _macro.cache_clear()
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
    return _regime()


# ---------- 자동매매봇 (BACKLOG #7, KIS 모의투자) ----------
@app.get("/api/bot/state")
def bot_state_get():
    return bot.get_state()


@app.post("/api/bot/toggle")
def bot_toggle(data: dict = Body(...)):
    bot.set_enabled(bool(data.get("enabled")))
    return {"ok": True, "enabled": bool(data.get("enabled"))}


@app.post("/api/bot/run")
def bot_run():
    """수동 1회 실행 — 실주문은 장 시간(평일 09:00~15:20 KST)에만 나간다."""
    return bot.run_once(dry_run=False)


@app.post("/api/bot/preview")
def bot_preview():
    """판단 미리보기(dry-run) — 주문 없이 '지금 무엇을 왜 매매할지' 계획만 계산. 장 시간 무관."""
    return bot.run_once(dry_run=True)


@app.post("/api/bot/reset")
def bot_reset():
    """봇 포지션·거래내역 초기화(설정 유지) — 과거 유령거래 등 정합성 깨진 상태 정리용."""
    db.bot_reset()
    return {"ok": True}


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


@app.get("/api/macro")
def macro_get():
    """미 거시 시황(CPI·기준금리·10년물·나스닥·VIX) + 우호/비우호 요약 — FRED 기반.
    signals/macro.py 참고. FRED_API_KEY 없으면 ready=False."""
    data = _macro()
    if not data["indicators"]:
        return {"ready": False, "indicators": []}
    return {"ready": True, **data}


# ---------- SPA 서빙 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")
