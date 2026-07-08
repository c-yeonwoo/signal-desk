"""캐시 로더 — apt-signal 컨벤션(parquet=시계열, json=메타/소형 데이터)을 그대로 따른다.

ingest 모듈은 데이터만 반환하고, 캐시 형식·경로 결정은 전부 이 파일이 담당한다.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import pandas as pd

from signal_desk.ingest import dart, fred, krx, krx_open_api

log = logging.getLogger("signal_desk.store")

CACHE_DIR = Path("data/cache")
UNIVERSE_FILE = CACHE_DIR / "universe.json"
PRICES_FILE = CACHE_DIR / "prices.parquet"
FUNDAMENTALS_FILE = CACHE_DIR / "fundamentals.json"
FUNDAMENTALS_HISTORY_FILE = CACHE_DIR / "fundamentals_history.json"  # point-in-time 백테스트용 연도별 재무
MACRO_FILE = CACHE_DIR / "macro.json"
MACRO_KR_FILE = CACHE_DIR / "macro_kr.json"  # 한국은행 ECOS 거시(기준금리·국고채·CPI)
GURUS_FILE = CACHE_DIR / "gurus.json"  # 거장 포트폴리오(SEC 13F) 스냅샷
US_UNIVERSE_FILE = CACHE_DIR / "us_universe.json"   # S&P500 구성종목(datahub)
US_PRICES_FILE = CACHE_DIR / "us_prices.parquet"    # 미국 종목 일봉(KIS 해외)
US_EXCHANGES_FILE = CACHE_DIR / "us_exchanges.json"  # ticker→KIS 거래소코드 캐시(탐지 비용 절약)
WARNINGS_FILE = CACHE_DIR / "warnings.json"  # 토스 투자경고·거래정지·과열·VI(매수 veto용)
US_FUNDAMENTALS_FILE = CACHE_DIR / "us_fundamentals.json"  # 미국 발행주식수·PER(Alpha Vantage, 소량 백필)
FLOWS_FILE = CACHE_DIR / "flows.json"  # 투자자별 수급(외국인·기관 순매수, KR) — 시그널 수급 팩터

PRICE_HISTORY_DAYS = 400  # MA120 워밍업 + 백테스트 여유분


def _write_json(path: Path, data) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_universe() -> list[dict]:
    items = krx.universe()
    _write_json(UNIVERSE_FILE, items)
    return items


def fetch_prices(universe: list[dict] | None = None, days: int = PRICE_HISTORY_DAYS) -> pd.DataFrame:
    universe = universe if universe is not None else load_universe()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    rows = []
    for item in universe:
        ticker = item["ticker"]
        try:
            bars = krx.ohlcv(ticker, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        except Exception as e:
            log.error("시세 수집 실패(%s): %s", ticker, e)
            continue
        for bar in bars:
            rows.append({"ticker": ticker, **bar})

    df = pd.DataFrame(rows, columns=["date", "ticker", "open", "close", "volume"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PRICES_FILE, index=False)
    return df


def fetch_flows(universe: list[dict] | None = None, days: int = 20) -> dict:
    """최근 days 거래일 투자자별 순매수(외국인·기관, KR)를 종목별로 수집 → flows.json.
    intensity = (외국인+기관 순매수) / 전체 매수대금 — 종목 규모 무관하게 [-1,1]로 자기정규화(수급 강도).
    pykrx 실패분은 건너뜀(그레이스풀). 반환: {ticker: {foreign_net, inst_net, intensity}}."""
    universe = universe if universe is not None else load_universe()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days * 1.6) + 5)  # 거래일 days개 확보 위해 달력일 여유
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    out: dict[str, dict] = {}
    for item in universe:
        ticker = item["ticker"]
        fl = krx.investor_flows(ticker, s, e)
        if not fl:
            continue
        net = fl["foreign_net"] + fl["inst_net"]
        tot = fl["total_buy"]
        intensity = max(-1.0, min(1.0, net / tot)) if tot else 0.0
        out[ticker] = {"foreign_net": fl["foreign_net"], "inst_net": fl["inst_net"],
                       "intensity": round(intensity, 4)}
    if out:
        _write_json(FLOWS_FILE, out)
    return out


def load_flows() -> dict[str, dict]:
    if not FLOWS_FILE.exists():
        return {}
    return json.loads(FLOWS_FILE.read_text(encoding="utf-8"))


def fetch_fundamentals(universe: list[dict] | None = None, bsns_year: str | None = None) -> dict:
    """DART 재무데이터(ROE/부채비율/매출성장) + KRX 시가총액을 결합해 PER/PBR까지 채운다.

    PER = 시가총액 / 당기순이익, PBR = 시가총액 / 자본총계 — 주당 지표(EPS/BPS)로 나눴다 곱하는
    것과 수학적으로 동일하지만 발행주식수 없이 바로 계산 가능해 더 안정적이다. 순이익이 적자면
    PER은 의미가 없어 계산하지 않는다(업계 관례).
    """
    universe = universe if universe is not None else load_universe()
    bsns_year = bsns_year or str(datetime.date.today().year - 1)  # 최신 사업보고서는 보통 전년도분

    codes = dart.corp_codes()
    if not codes:
        log.warning("DART_API_KEY 미설정 — 기본적분석 생략(기술점수만 사용)")
        _write_json(FUNDAMENTALS_FILE, {})
        return {}

    mktcaps = krx_open_api.market_caps()
    if not mktcaps:
        log.warning("KRX 시가총액 조회 실패(키 없음/서비스 미승인) — PER/PBR 생략, ROE 등만 사용")

    out: dict[str, dict] = {}
    for item in universe:
        ticker = item["ticker"]
        corp_code = codes.get(ticker)
        if not corp_code:
            continue
        metrics = dart.fundamentals(ticker, corp_code, bsns_year)
        if not metrics:
            continue

        mktcap = mktcaps.get(ticker)
        net_income = metrics.get("net_income")
        equity = metrics.get("equity")
        if mktcap:
            metrics["mktcap"] = mktcap  # 현재 시가총액(원) — 시그널 리스트 정렬·차트 헤더 표기용
            if net_income and net_income > 0:
                metrics["per"] = round(mktcap / net_income, 2)
            if equity and equity > 0:
                metrics["pbr"] = round(mktcap / equity, 2)
        out[ticker] = metrics
    _write_json(FUNDAMENTALS_FILE, out)
    return out


def update_valuation() -> int:
    """캐시된 DART 재무(net_income/equity)에 '오늘 시총'만 다시 붙여 PER/PBR·시총을 재계산한다.
    DART 재호출 없이 KRX 시총 1콜만 — 연간 재무는 분기에나 바뀌지만 PER/PBR·시총은 가격 따라
    매일 변하므로, 무거운 DART 수집은 분기 1회로 두고 이 함수로 매일 밸류만 갱신한다. 반환: 갱신 종목 수."""
    fund = load_fundamentals()
    if not fund:
        return 0
    mktcaps = krx_open_api.market_caps()
    if not mktcaps:
        log.warning("KRX 시가총액 조회 실패 — PER/PBR·시총 갱신 스킵(기존값 유지)")
        return 0
    n = 0
    for ticker, m in fund.items():
        mc = mktcaps.get(ticker)
        if not mc:
            continue
        m["mktcap"] = mc
        ni, eq = m.get("net_income"), m.get("equity")
        m["per"] = round(mc / ni, 2) if (ni and ni > 0) else None
        m["pbr"] = round(mc / eq, 2) if (eq and eq > 0) else None
        n += 1
    _write_json(FUNDAMENTALS_FILE, fund)
    return n


def fetch_fundamentals_history(universe: list[dict] | None = None,
                               years: list[str] | None = None) -> dict:
    """연도별 재무(ROE/부채/성장 + net_income/equity)를 수집 — point-in-time 백테스트용.

    반환·저장 형태: {ticker: {year: metrics}}. 각 연도 사업보고서는 이듬해 초에 공시되므로
    백테스트가 '그 시점에 알 수 있던' 재무만 쓰도록 backtest가 연도→가용일 매핑을 적용한다.
    PER/PBR은 시점별 시가가 필요해 여기 저장하지 않는다(backtest에서 그때 가격으로 계산).
    """
    universe = universe if universe is not None else load_universe()
    if years is None:
        this_year = datetime.date.today().year
        years = [str(this_year - n) for n in (1, 2, 3)]  # 최근 3개 사업연도

    codes = dart.corp_codes()
    if not codes:
        log.warning("DART_API_KEY 미설정 — point-in-time 재무 수집 생략")
        _write_json(FUNDAMENTALS_HISTORY_FILE, {})
        return {}

    out: dict[str, dict] = {}
    for item in universe:
        ticker = item["ticker"]
        corp_code = codes.get(ticker)
        if not corp_code:
            continue
        by_year: dict[str, dict] = {}
        for y in years:
            metrics = dart.fundamentals(ticker, corp_code, y)
            if metrics:
                by_year[y] = metrics
        if by_year:
            out[ticker] = by_year
    _write_json(FUNDAMENTALS_HISTORY_FILE, out)
    return out


def load_fundamentals_history() -> dict[str, dict]:
    if not FUNDAMENTALS_HISTORY_FILE.exists():
        return {}
    return json.loads(FUNDAMENTALS_HISTORY_FILE.read_text(encoding="utf-8"))


def fetch_macro() -> list[dict]:
    """FRED 거시 지표(CPI/금리/나스닥/VIX)를 수집해 캐시. 키 없으면 빈 리스트."""
    items = fred.macro_indicators()
    _write_json(MACRO_FILE, items)
    return items


def fetch_macro_kr() -> list[dict]:
    """한국은행 ECOS 거시(기준금리·국고채10년·CPI)를 수집해 캐시. 키 없으면 빈 리스트."""
    from signal_desk.ingest import ecos
    items = ecos.macro_indicators()
    _write_json(MACRO_KR_FILE, items)
    return items


def load_macro_kr() -> list[dict]:
    if not MACRO_KR_FILE.exists():
        return []
    return json.loads(MACRO_KR_FILE.read_text(encoding="utf-8"))


def fetch_gurus(top: int = 10) -> list[dict]:
    """거장 큐레이션의 최신 13F 보유내역을 수집·캐시. 조회 실패한 인물은 건너뛴다.
    반환·저장: [{key, name, desc, period, total_usd, n_holdings, holdings:[...]}]."""
    from signal_desk.ingest import edgar
    from signal_desk.reference import gurus as gref
    out = []
    for g in gref.all_gurus():
        h = edgar.holdings_13f(g["cik"], top=top)
        if not h:
            log.warning("거장 13F 조회 실패, 제외: %s", g["name"])
            continue
        out.append({"key": g["key"], "name": g["name"], "desc": g["desc"], **h})
    _write_json(GURUS_FILE, out)
    return out


def load_gurus() -> list[dict]:
    if not GURUS_FILE.exists():
        return []
    return json.loads(GURUS_FILE.read_text(encoding="utf-8"))


# ---------- 미국 주식(S&P500) — KIS 해외 시세, KOSPI와 별도 캐시로 격리 ----------
def fetch_us_universe() -> list[dict]:
    """S&P500 구성종목(datahub) 저장. [{ticker, name, sector}]."""
    from signal_desk.ingest import us
    items = us.sp500_constituents()
    if items:
        _write_json(US_UNIVERSE_FILE, items)
    return items


def load_us_universe() -> list[dict]:
    if not US_UNIVERSE_FILE.exists():
        return []
    return json.loads(US_UNIVERSE_FILE.read_text(encoding="utf-8"))


def _load_us_exchanges() -> dict:
    if not US_EXCHANGES_FILE.exists():
        return {}
    return json.loads(US_EXCHANGES_FILE.read_text(encoding="utf-8"))


def fetch_us_prices(tickers: list[str], days: int = 400) -> int:
    """지정 티커들의 미국 일봉을 KIS로 수집해 us_prices.parquet에 병합(upsert). 반환: 성공 종목 수.

    거래소코드(EXCD)는 탐지 결과를 us_exchanges.json에 캐시해 재탐지를 피한다. 기존 parquet에
    있던 다른 종목은 보존하고, 이번에 받은 종목만 갱신한다."""
    from signal_desk.ingest import toss, us
    use_toss = toss.available()  # 토스 우선(KR+US 단일·표준443·안정) → 미설정 시 KIS 폴백
    exch = _load_us_exchanges()
    existing = pd.read_parquet(US_PRICES_FILE) if US_PRICES_FILE.exists() else pd.DataFrame()
    frames = [existing[existing["ticker"].isin(tickers) == False]] if not existing.empty else []
    ok = 0
    for t in tickers:
        bars = toss.daily_ohlcv(t, count=min(days, 200)) if use_toss else None
        if not bars:  # 토스 미설정·실패 시 KIS 해외로 폴백
            excd = exch.get(t) or us.detect_exchange(t)
            if not excd:
                log.warning("US 거래소 탐지 실패, 제외: %s", t)
                continue
            exch[t] = excd
            bars = us.us_ohlcv(t, days=days, excd=excd)
        if not bars:
            continue
        frames.append(pd.DataFrame([{"ticker": t, **b} for b in bars]))
        ok += 1
    if frames:
        df = pd.concat(frames, ignore_index=True)[["date", "ticker", "open", "close", "volume"]]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(US_PRICES_FILE, index=False)
    _write_json(US_EXCHANGES_FILE, exch)
    return ok


def load_us_price_series() -> dict[str, list[float]]:
    if not US_PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(US_PRICES_FILE).sort_values(["ticker", "date"])
    return _overlay_closes({t: g["close"].tolist() for t, g in df.groupby("ticker")})


def load_us_quotes() -> dict[str, dict]:
    """US 종목별 최신 거래량·20일 평균 거래량(정렬·표기용). 시총은 데이터 소스 없어 미제공."""
    if not US_PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(US_PRICES_FILE)
    if df.empty or "volume" not in df.columns:
        return {}
    df = df.sort_values(["ticker", "date"])
    out = {}
    for t, g in df.groupby("ticker"):
        vols = g["volume"].tolist()
        out[t] = {"vol": float(vols[-1]) if vols else None,
                  "vol_avg": round(sum(vols[-20:]) / len(vols[-20:])) if vols else None}
    return out


def load_us_fundamentals() -> dict[str, dict]:
    """US 종목 재무 캐시 {ticker: {shares, per, sector}} — Alpha Vantage 백필분."""
    if not US_FUNDAMENTALS_FILE.exists():
        return {}
    return json.loads(US_FUNDAMENTALS_FILE.read_text(encoding="utf-8"))


def fetch_us_fundamentals(tickers: list[str], max_calls: int = 20) -> int:
    """아직 캐시에 없는 US 종목의 발행주식수·PER를 Alpha Vantage로 소량씩 백필(하루 25콜 한도).
    한 번에 max_calls개만 채우고 나머지는 다음 실행에서 이어감. 채운 개수 반환."""
    from signal_desk.ingest import alphavantage
    cache = load_us_fundamentals()
    todo = [t for t in tickers if t not in cache][:max_calls]
    got = 0
    for t in todo:
        ov = alphavantage.overview(t)
        if ov is None:  # 키 없음·한도 초과 → 중단(다음에 이어서)
            break
        cache[t] = {"shares": ov["shares"], "per": ov["per"], "sector": ov["sector"]}
        got += 1
    if got:
        _write_json(US_FUNDAMENTALS_FILE, cache)
    return got


def fetch_us_shares_toss(tickers: list[str]) -> int:
    """토스 종목마스터로 US 발행주식수를 배치(200) 수집해 us_fundamentals 캐시에 병합.
    Alpha Vantage 25콜/일 병목 없이 전 종목 시총 계산 가능(PER은 EPS가 없어 AV 유지)."""
    from signal_desk.ingest import toss
    if not toss.available():
        return 0
    cache = load_us_fundamentals()
    master = toss.stocks(tickers)
    got = 0
    for t, m in master.items():
        so = m.get("shares_outstanding")
        if not so:
            continue
        cache.setdefault(t, {"per": None, "sector": None})
        cache[t]["shares"] = so
        got += 1
    if got:
        _write_json(US_FUNDAMENTALS_FILE, cache)
    return got


def fetch_warnings(tickers: list[str]) -> int:
    """토스 투자경고/거래정지/과열/VI를 종목별 조회해 warnings.json에 캐시(활성 유형만).
    매수 가드레일(veto)이 이 집합을 근거로 씀. 토스 미설정 시 0."""
    from signal_desk.ingest import toss
    if not toss.available():
        return 0
    out = {t: w for t in tickers if (w := toss.warnings(t))}
    _write_json(WARNINGS_FILE, out)
    return len(out)


def load_warned_tickers() -> set[str]:
    """활성 투자경고·거래정지 등이 걸린 종목 집합(매수 veto용). 없으면 빈 집합."""
    if not WARNINGS_FILE.exists():
        return set()
    return set(json.loads(WARNINGS_FILE.read_text(encoding="utf-8")).keys())


def fetch_us_fundamentals_edgar(tickers: list[str], max_calls: int = 40) -> int:
    """EDGAR XBRL companyfacts로 US 순이익·자기자본을 백필 → us_fundamentals 병합(PER/PBR 계산용).
    이미 net_income/equity 있는 종목은 스킵해 점진 백필. 한 번에 최대 max_calls 종목만(스로틀). 시도 수 반환."""
    from signal_desk.ingest import edgar
    cache = load_us_fundamentals()
    done = 0
    for t in tickers:
        if done >= max_calls:
            break
        cur = cache.get(t) or {}
        if cur.get("net_income") is not None or cur.get("equity") is not None:
            continue  # 이미 채워짐
        f = edgar.fundamentals(t)
        done += 1  # 호출 시도 카운트(스로틀)
        if not f:
            continue
        cache.setdefault(t, {"shares": None, "per": None, "sector": None})
        cache[t]["net_income"] = f.get("net_income")
        cache[t]["equity"] = f.get("equity")
    if done:
        _write_json(US_FUNDAMENTALS_FILE, cache)
    return done


def us_marketcaps(prices: dict[str, list[float]] | None = None) -> dict[str, dict]:
    """US 종목별 시총·PER·PBR — 시총은 발행주식수×최신종가로 매일 재계산. PER/PBR은 EDGAR 순이익·
    자기자본이 있으면 시총으로 계산(없으면 AV의 per 폴백)."""
    fund = load_us_fundamentals()
    if not fund:
        return {}
    prices = prices if prices is not None else load_us_price_series()
    out = {}
    for t, f in fund.items():
        shares, closes = f.get("shares"), prices.get(t)
        mktcap = round(shares * closes[-1]) if shares and closes else None
        ni, eq = f.get("net_income"), f.get("equity")
        per = round(mktcap / ni, 2) if (mktcap and ni and ni > 0) else f.get("per")
        pbr = round(mktcap / eq, 2) if (mktcap and eq and eq > 0) else None
        out[t] = {"mktcap": mktcap, "per": per, "pbr": pbr}
    return out


def load_us_price_history(ticker: str) -> list[dict]:
    if not US_PRICES_FILE.exists():
        return []
    df = pd.read_parquet(US_PRICES_FILE)
    df = df[df["ticker"] == ticker].sort_values("date")
    return [{"date": r["date"], "close": float(r["close"])} for _, r in df.iterrows()]


def load_universe() -> list[dict]:
    if not UNIVERSE_FILE.exists():
        return []
    return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))


def load_macro() -> list[dict]:
    if not MACRO_FILE.exists():
        return []
    return json.loads(MACRO_FILE.read_text(encoding="utf-8"))


# 장중 실시간 현재가 오버레이 — 무거운 refresh 없이 종가 시계열 마지막에 '잠정봉' 1개를 얹어
# 시그널·봇·페이퍼 체결가를 현재가 기준으로 돌린다(장 마감 후엔 clear → 종가 복귀). 파일엔 안 쓴다.
_LIVE_QUOTES: dict[str, float] = {}
_LIVE_TS: float | None = None  # 마지막 '성공' 갱신 시각(epoch)
_LIVE_ATTEMPT: dict = {"ts": None, "result": None, "markets": []}  # 마지막 '시도' 시각·결과(성공이든 실패든)


def note_live_attempt(result: str, markets: list[str] | None = None) -> None:
    """실시간가 갱신 '시도'를 기록 — 성공/실패 무관하게 언제 시도했고 결과가 뭔지 남긴다.
    result: ok | no_quotes(토스 응답 빔·토큰실패) | toss_off(키 없음) | closed(장외)."""
    _LIVE_ATTEMPT["ts"] = datetime.datetime.now(datetime.timezone.utc).timestamp()
    _LIVE_ATTEMPT["result"] = result
    _LIVE_ATTEMPT["markets"] = list(markets or [])


def set_live_quotes(quotes: dict[str, float]) -> None:
    """실시간 현재가 오버레이 설정(양수만). 빈 dict면 오버레이 없음."""
    global _LIVE_TS
    _LIVE_QUOTES.clear()
    for k, v in (quotes or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            _LIVE_QUOTES[k] = fv
    _LIVE_TS = datetime.datetime.now(datetime.timezone.utc).timestamp() if _LIVE_QUOTES else None


def clear_live_quotes() -> None:
    global _LIVE_TS
    _LIVE_QUOTES.clear()
    _LIVE_TS = None


def live_status() -> dict:
    """실시간가 오버레이 상태 — 성공 갱신 시각 + 마지막 시도 시각·결과. 왜 안 바뀌는지 진단용."""
    return {"on": bool(_LIVE_QUOTES), "count": len(_LIVE_QUOTES), "updated": _LIVE_TS,
            "attempt_ts": _LIVE_ATTEMPT["ts"], "attempt_result": _LIVE_ATTEMPT["result"],
            "attempt_markets": _LIVE_ATTEMPT["markets"]}


def _overlay_closes(series: dict[str, list[float]]) -> dict[str, list[float]]:
    """live 현재가가 있으면 각 종목 종가열 끝에 잠정봉 1개 append(길이 +1). 없으면 원본."""
    if not _LIVE_QUOTES:
        return series
    return {t: (closes + [_LIVE_QUOTES[t]]) if (_LIVE_QUOTES.get(t) and closes) else closes
            for t, closes in series.items()}


def load_price_series() -> dict[str, list[float]]:
    """ticker -> 종가 리스트(오래된→최신). engine.evaluate()/backtest_summary()에 바로 투입 가능.
    장중 실시간가가 설정돼 있으면 마지막에 잠정봉 1개를 얹는다(set_live_quotes)."""
    if not PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return {}
    df = df.sort_values(["ticker", "date"])
    return _overlay_closes({ticker: g["close"].tolist() for ticker, g in df.groupby("ticker")})


def load_dates_by_ticker() -> dict[str, list[str]]:
    """ticker -> 날짜 리스트(오래된→최신) — load_price_series()와 동일 정렬. point-in-time 백테스트용."""
    if not PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return {}
    df = df.sort_values(["ticker", "date"])
    dates = {ticker: [str(d) for d in g["date"].tolist()] for ticker, g in df.groupby("ticker")}
    if _LIVE_QUOTES:  # load_price_series의 잠정봉과 길이 정합 유지(백테스트 date-close 짝 안 깨지게)
        today = datetime.date.today().isoformat()
        dates = {t: (ds + [today]) if (_LIVE_QUOTES.get(t) and ds) else ds for t, ds in dates.items()}
    return dates


def load_price_history(ticker: str) -> list[dict]:
    """단일 종목의 (date, close) 시계열(오래된→최신) — 차트용, 날짜를 유지한다."""
    if not PRICES_FILE.exists():
        return []
    df = pd.read_parquet(PRICES_FILE)
    df = df[df["ticker"] == ticker].sort_values("date")
    if df.empty:
        return []
    return [{"date": row["date"], "close": float(row["close"])} for _, row in df.iterrows()]


def load_index_history() -> list[dict]:
    """유니버스 종가로 만든 동일가중 정규화 지수(코스피200 근사) — [{date, close}].

    코스피 종합지수 원본 API가 없어, 전 구간 존재하는 종목들을 시작일 100으로 정규화해
    평균낸 동일가중 지수로 근사한다(시장 전체 흐름 참고용). 정확한 지수가 필요하면
    data.krx.co.kr 지수 데이터로 교체.
    """
    if not PRICES_FILE.exists():
        return []
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return []
    piv = df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    piv = piv.dropna(axis=1)  # 전 구간 존재하는 종목만(정렬·정규화용)
    if piv.empty:
        return []
    normalized = piv / piv.iloc[0] * 100.0
    idx = normalized.mean(axis=1)
    return [{"date": str(d), "close": round(float(v), 2)} for d, v in idx.items()]


def load_fundamentals() -> dict[str, dict]:
    if not FUNDAMENTALS_FILE.exists():
        return {}
    return json.loads(FUNDAMENTALS_FILE.read_text(encoding="utf-8"))


def load_quotes(vol_window: int = 20) -> dict[str, dict]:
    """종목별 시세 요약 — {ticker: {price, prev_close, change_pct, mktcap, vol, vol_avg}}.

    price=최신 종가, change_pct=전일 대비, mktcap=fundamentals의 시가총액(원, 없으면 None),
    vol=최신 거래량, vol_avg=최근 vol_window일 평균 거래량. 구 parquet(거래량 컬럼 없음)면
    vol/vol_avg는 None으로 그레이스풀 폴백(재수집 전까지 UI는 '—' 표시).
    """
    if not PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return {}
    has_vol = "volume" in df.columns
    fundamentals = load_fundamentals()
    df = df.sort_values(["ticker", "date"])
    out: dict[str, dict] = {}
    for ticker, g in df.groupby("ticker"):
        closes = g["close"].tolist()
        # 장중 실시간가가 있으면 현재가=live, 전일=마지막 종가(오늘 잠정봉의 직전 = 어제 종가)
        live = _LIVE_QUOTES.get(ticker)
        price = float(live) if live else float(closes[-1])
        prev = float(closes[-1]) if live else (float(closes[-2]) if len(closes) > 1 else price)
        vol = vol_avg = None
        if has_vol:
            vols = [float(v) for v in g["volume"].tolist() if v == v]  # NaN 제외
            if vols:
                vol = vols[-1]
                vol_avg = round(sum(vols[-vol_window:]) / len(vols[-vol_window:]), 1)
        out[ticker] = {
            "price": round(price, 2),
            "prev_close": round(prev, 2),
            "change_pct": round((price / prev - 1) * 100, 2) if prev else 0.0,
            "mktcap": (fundamentals.get(ticker) or {}).get("mktcap"),
            "vol": vol,
            "vol_avg": vol_avg,
        }
    return out


def is_ready() -> bool:
    return PRICES_FILE.exists() and UNIVERSE_FILE.exists()


# 데이터 신뢰도 진단용 앵커(대형주) — 캐시 종가 vs 토스 실시간가 비율로 스케일/합성 여부 판정.
_SANITY_TICKERS = ["005930", "000660", "005380", "035420", "005490"]


def price_sanity(tickers: list[str] | None = None) -> dict:
    """캐시 종가와 토스 실시간가의 비율로 시세 데이터가 '실제 스케일'인지 진단한다.
    ratio(캐시/실시간)≈1이면 실데이터, 종목별로 크게(>15%) 벗어나면 스케일·합성 의심.
    토스 미연동이면 비교 불가(캐시값만 반환). track record 신뢰의 전제 점검용."""
    from signal_desk.ingest import toss
    tickers = tickers or _SANITY_TICKERS
    if not PRICES_FILE.exists():
        return {"ok": False, "reason": "시세 캐시 없음"}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return {"ok": False, "reason": "시세 캐시 비어있음"}
    df = df.sort_values(["ticker", "date"])
    cached = {t: float(g["close"].tolist()[-1]) for t, g in df.groupby("ticker") if len(g)}
    if not toss.available():
        return {"ok": False, "toss": False, "reason": "토스 미연동 — 실시간가와 비교 불가(캐시값만 표시)",
                "rows": [{"ticker": t, "cached": cached.get(t), "live": None, "ratio": None} for t in tickers]}
    live = toss.prices(tickers)
    rows = []
    for t in tickers:
        c, l = cached.get(t), live.get(t)
        rows.append({"ticker": t, "cached": c, "live": l,
                     "ratio": round(c / l, 3) if (c and l) else None})
    ratios = [r["ratio"] for r in rows if r["ratio"]]
    scaled_suspect = bool(ratios) and any(abs(x - 1) > 0.15 for x in ratios)  # 15%↑ 벗어나면 의심
    return {"ok": True, "toss": True, "scaled_suspect": scaled_suspect,
            "verdict": "스케일/합성 의심 — 실데이터 교체 필요" if scaled_suspect else "실데이터로 판단(비율≈1)",
            "rows": rows}
