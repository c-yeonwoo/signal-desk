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
MACRO_FILE = CACHE_DIR / "macro.json"

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

    df = pd.DataFrame(rows, columns=["date", "ticker", "open", "close"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PRICES_FILE, index=False)
    return df


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
            if net_income and net_income > 0:
                metrics["per"] = round(mktcap / net_income, 2)
            if equity and equity > 0:
                metrics["pbr"] = round(mktcap / equity, 2)
        out[ticker] = metrics
    _write_json(FUNDAMENTALS_FILE, out)
    return out


def fetch_macro() -> list[dict]:
    """FRED 거시 지표(CPI/금리/나스닥/VIX)를 수집해 캐시. 키 없으면 빈 리스트."""
    items = fred.macro_indicators()
    _write_json(MACRO_FILE, items)
    return items


def load_universe() -> list[dict]:
    if not UNIVERSE_FILE.exists():
        return []
    return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))


def load_macro() -> list[dict]:
    if not MACRO_FILE.exists():
        return []
    return json.loads(MACRO_FILE.read_text(encoding="utf-8"))


def load_price_series() -> dict[str, list[float]]:
    """ticker -> 종가 리스트(오래된→최신). engine.evaluate()/backtest_summary()에 바로 투입 가능."""
    if not PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty:
        return {}
    df = df.sort_values(["ticker", "date"])
    return {ticker: g["close"].tolist() for ticker, g in df.groupby("ticker")}


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


def is_ready() -> bool:
    return PRICES_FILE.exists() and UNIVERSE_FILE.exists()
