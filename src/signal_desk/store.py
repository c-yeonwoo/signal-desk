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
US_EARNINGS_FILE = CACHE_DIR / "us_earnings_calendar.json"  # 미국 실적발표 예정일(Alpha Vantage, 벌크 1콜/일)
FLOWS_FILE = CACHE_DIR / "flows.json"  # 투자자별 수급(외국인·기관 순매수, KR) — 시그널 수급 팩터
SHORT_FILE = CACHE_DIR / "short.json"  # 종목별 공매도 거래비중(KRX, KR) — 시그널 공매도 팩터
CONSENSUS_HISTORY_FILE = CACHE_DIR / "consensus_history.parquet"  # 애널 컨센서스 일별 PIT 스냅샷(목표주가·투자의견·선행EPS, KR) — 리비전/목표가v2용, 축적만(미반영)
MARKET_FLOW_FILE = CACHE_DIR / "market_flow.json"  # 시장 전체(KOSPI) 외국인·기관 순매수 누적(토스) — 국면 신호
SHORTFORM_BG_FILE = CACHE_DIR / "shortform_bg.img"  # 숏폼 카드 배경 업로드 원본(1장) — data URI 대신 짧은 URL로 서빙
COMPANY_PROFILES_FILE = CACHE_DIR / "company_profiles.json"  # DART 기업개황(설립연도·대표·영문명) — 숏폼 기업 소개
SIGNAL_HISTORY_FILE = CACHE_DIR / "signal_history.parquet"  # 일별 종목 시그널·팩터 스냅샷(PIT) — 향후 팩터 백테스트용

PRICE_HISTORY_DAYS = 1825  # 약 5년 — 모멘텀(60일 최강)·다중국면 팩터/백테스트 신뢰도. 최초 1회 전량, 이후 증분


def _write_json(path: Path, data) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_universe() -> list[dict]:
    items = krx.universe()
    _write_json(UNIVERSE_FILE, items)
    return items


def fetch_prices(universe: list[dict] | None = None, days: int = PRICE_HISTORY_DAYS,
                 full: bool = False) -> pd.DataFrame:
    """유니버스 일봉 수집 → prices.parquet(upsert). 기본은 **증분**(각 종목 마지막 저장일부터
    오늘까지만 재수집해 append)이라 매일 갱신이 가볍다(5년치 200종목 재수집 방지). full=True거나
    기존 데이터가 없는 종목은 days만큼 전량 백필한다. (ticker,date) 중복은 keep='last'로 제거 —
    마지막 저장일을 재수집해 잠정 종가를 확정치로 덮는다.

    ※ 최초 5년 백필은 한 번 `full=True`(CLI `sigdesk fetch --full`)로 돌린 뒤, 이후 데일리 루프는
    증분으로 유지한다. (액면분할 등 소급 수정주가 반영은 주기적 full 재수집 필요 — 후속 과제.)"""
    universe = universe if universe is not None else load_universe()
    end = datetime.date.today()
    existing = pd.read_parquet(PRICES_FILE) if PRICES_FILE.exists() else None
    has_existing = existing is not None and not existing.empty
    last_by_ticker = (existing.groupby("ticker")["date"].max().to_dict() if has_existing else {})
    rows = []
    for item in universe:
        ticker = item["ticker"]
        last = None if full else last_by_ticker.get(ticker)
        start = datetime.date.fromisoformat(str(last)[:10]) if last else end - datetime.timedelta(days=days)
        if start > end:
            continue  # 이미 최신(오늘 이후 시작일 없음)
        try:
            bars = krx.ohlcv(ticker, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        except Exception as e:
            log.error("시세 수집 실패(%s): %s", ticker, e)
            continue
        for bar in bars:
            rows.append({"ticker": ticker, **bar})

    new = pd.DataFrame(rows, columns=["date", "ticker", "open", "close", "volume"])
    combined = pd.concat([existing, new], ignore_index=True) if has_existing else new
    combined = (combined.drop_duplicates(subset=["ticker", "date"], keep="last")
                .sort_values(["ticker", "date"]).reset_index(drop=True))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(PRICES_FILE, index=False)
    return combined


def fetch_flows(universe: list[dict] | None = None, days: int = 20) -> dict:
    """최근 days 거래일 투자자별 순매수(외국인·기관, KR)를 종목별로 수집 → flows.json.
    intensity = (외국인+기관 순매수) / 전체 거래량 — 종목 규모 무관하게 [-1,1]로 자기정규화(수급 강도).
    소스: 네이버 금융(pykrx 투자자 엔드포인트가 KRX 스키마 변경으로 죽어 대체). 실패분 건너뜀."""
    from signal_desk.ingest import naver
    universe = universe if universe is not None else load_universe()
    out: dict[str, dict] = {}
    fails = 0
    for i, item in enumerate(universe):
        ticker = item["ticker"]
        fl = naver.investor_flow(ticker, days)
        if not fl:
            fails += 1
            # 서킷브레이커: 앞 종목이 연달아 전부 실패하면 소스가 통째로 막힌 것(IP 차단 등) →
            # 200종목 다 두드리지 않고 조기 중단, 로그 1줄. 수급 팩터는 데이터 없음으로 자동 제외.
            if out == {} and fails >= 8:
                log.warning("수급 수집 중단 — 네이버 투자자 수급 응답 없음(%d/%d 연속 실패). "
                            "수급 팩터는 데이터 없음으로 자동 제외됩니다(다른 팩터엔 영향 없음).", fails, len(universe))
                return out
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


def _volume_by_ticker_date() -> dict[str, dict[str, float]]:
    """{ticker: {date: 총거래량}} — 공매도 비중 계산용(우리 KR OHLCV의 volume)."""
    if not PRICES_FILE.exists():
        return {}
    df = pd.read_parquet(PRICES_FILE)
    if df.empty or "volume" not in df.columns:
        return {}
    out: dict[str, dict[str, float]] = {}
    for t, g in df.groupby("ticker"):
        out[str(t)] = {str(d): float(v) for d, v in zip(g["date"], g["volume"])}
    return out


def fetch_short(universe: list[dict] | None = None, days: int = 20) -> dict:
    """최근 days 거래일 종목별 공매도 거래비중(KR)을 수집 → short.json.
    short_ratio = Σ공매도거래량 / Σ총거래량 (둘 다 주수 → 종목 규모·스케일 시세 무관).
    소스: KRX 외부용 엔드포인트(ingest.krx_short). 공매도량은 KRX, 총거래량은 우리 OHLCV(동일 KRX 원천)."""
    from signal_desk.ingest import krx_short
    universe = universe if universe is not None else load_universe()
    vol_by = _volume_by_ticker_date()
    out: dict[str, dict] = {}
    fails = 0
    for item in universe:
        ticker = item["ticker"]
        sv = krx_short.short_volume(ticker, days)
        if not sv:
            fails += 1
            # 서킷브레이커: 소스가 통째로 막히면(스키마 변경 등) 조기 중단. 공매도 팩터는 자동 제외.
            if out == {} and fails >= 8:
                log.warning("공매도 수집 중단 — KRX 응답 없음(%d/%d 연속 실패). "
                            "공매도 팩터는 데이터 없음으로 자동 제외됩니다(다른 팩터엔 영향 없음).", fails, len(universe))
                return out
            continue
        fails = 0
        vmap = vol_by.get(ticker, {})
        matched = [(sv[d], vmap[d]) for d in sv if d in vmap and vmap[d]]
        svol = sum(s for s, _ in matched)
        tvol = sum(v for _, v in matched)
        if not matched or not tvol:
            continue
        out[ticker] = {"short_ratio": round(svol / tvol, 4),
                       "short_vol": round(svol), "total_vol": round(tvol), "days": len(matched)}
    if out:
        _write_json(SHORT_FILE, out)
    return out


def load_short() -> dict[str, dict]:
    if not SHORT_FILE.exists():
        return {}
    return json.loads(SHORT_FILE.read_text(encoding="utf-8"))


def _consensus_row(ticker: str, date: str, c: dict) -> dict:
    """컨센서스 스냅샷 1행(flat) — 선행연도는 최대 2개(가까운 순)만 컬럼화."""
    fwds = sorted(c.get("forwards") or [], key=lambda f: f["year"])[:2]
    row = {"date": date, "ticker": ticker,
           "price_target_mean": c.get("price_target_mean"),
           "recomm_mean": c.get("recomm_mean"),
           "source_date": c.get("source_date"),
           "fwd1_year": None, "fwd1_eps": None, "fwd2_year": None, "fwd2_eps": None}
    for i, f in enumerate(fwds, 1):
        row[f"fwd{i}_year"], row[f"fwd{i}_eps"] = f["year"], f["eps"]
    return row


def fetch_consensus(universe: list[dict] | None = None, date: str | None = None) -> int:
    """오늘의 애널 컨센서스(목표주가·투자의견·선행EPS)를 종목별로 수집해 PIT 시계열에 append.
    소스: 네이버(ingest.naver.consensus). 같은 날 재실행은 그 날짜를 덮어쓴다. 반환: 기록 행수.

    ⚠️ 이 데이터는 '수집만' 한다 — 시그널·목표가 계산엔 아직 반영하지 않는다. 리비전(Δ)은 시계열이
    충분히 쌓인 뒤 계산해야 의미가 있고, 목표가 v2도 검증 후 반영한다(현재 동작 무영향)."""
    from signal_desk.ingest import naver
    universe = universe if universe is not None else load_universe()
    date = date or datetime.date.today().isoformat()
    rows, fails = [], 0
    for item in universe:
        ticker = item["ticker"]
        c = naver.consensus(ticker)
        if not c:
            fails += 1
            if not rows and fails >= 8:  # 서킷브레이커: 소스 통째로 막히면 조기 중단
                log.warning("컨센서스 수집 중단 — 네이버 응답 없음(%d/%d 연속 실패).", fails, len(universe))
                return 0
            continue
        fails = 0
        rows.append(_consensus_row(ticker, date, c))
    if not rows:
        return 0
    df_new = pd.DataFrame(rows)
    if CONSENSUS_HISTORY_FILE.exists():
        old = pd.read_parquet(CONSENSUS_HISTORY_FILE)
        old = old[old["date"] != date]  # 같은 날 재실행 → 갱신
        df_new = pd.concat([old, df_new], ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df_new.to_parquet(CONSENSUS_HISTORY_FILE, index=False)
    return len(rows)


def load_consensus_history():
    if not CONSENSUS_HISTORY_FILE.exists():
        return pd.DataFrame()
    return pd.read_parquet(CONSENSUS_HISTORY_FILE)


def load_consensus_latest() -> dict[str, dict]:
    """종목별 가장 최근 컨센서스 스냅샷 {ticker: row}. (목표가 v2 등에서 '현재 수준'이 필요할 때용)"""
    df = load_consensus_history()
    if df.empty:
        return {}
    latest = df.sort_values("date").groupby("ticker").tail(1)
    return {r["ticker"]: {k: r[k] for k in df.columns} for _, r in latest.iterrows()}


def _market_flow_summary(records: list[dict]) -> dict:
    """토스 시장전체 투자자 거래 레코드(최신→과거) → 외국인·기관 순매수 5/20일 누적(원, 조원 환산).
    smart_net = 외국인+기관(스마트머니). 순수함수(테스트 분리)."""
    def cum(key: str, n: int) -> float:
        return sum(r.get(key, 0.0) for r in records[:n])
    fo5, fo20 = cum("foreigner_net", 5), cum("foreigner_net", 20)
    in5, in20 = cum("institution_net", 5), cum("institution_net", 20)
    to_jo = lambda v: round(v / 1e12, 3)  # 원 → 조원(표시용)
    return {
        "as_of": records[0]["date"] if records else None,
        "days": len(records),
        "foreign_net_5d": to_jo(fo5), "foreign_net_20d": to_jo(fo20),
        "inst_net_5d": to_jo(in5), "inst_net_20d": to_jo(in20),
        "smart_net_5d": to_jo(fo5 + in5), "smart_net_20d": to_jo(fo20 + in20),
    }


def fetch_market_flow() -> dict:
    """토스 시장 전체(KOSPI) 외국인·기관 순매수 누적 → market_flow.json. 국면(regime) 신호용.
    pykrx 종목별 수급이 죽어 그 대체로 '시장 전체'만 받는다(토스엔 종목별 없음). 미인증/실패 시 빈 dict."""
    from signal_desk.ingest import toss
    if not toss.available():
        return {}
    out: dict[str, dict] = {}
    for market in ("KOSPI",):
        recs = toss.market_investor_trading(market, "1d", 20)
        if recs:
            out[market] = _market_flow_summary(recs)
    if out:
        _write_json(MARKET_FLOW_FILE, out)
    return out


def load_market_flow() -> dict[str, dict]:
    if not MARKET_FLOW_FILE.exists():
        return {}
    return json.loads(MARKET_FLOW_FILE.read_text(encoding="utf-8"))


def fetch_company_profiles(universe: list[dict] | None = None) -> dict:
    """DART 기업개황(설립연도·대표·영문명) 수집 → company_profiles.json. 프로필은 거의 불변이라
    이미 받은 종목은 건너뛴다(증분). 키 없음/조회 실패분은 스킵(그레이스풀)."""
    universe = universe if universe is not None else load_universe()
    codes = dart.corp_codes()
    if not codes:
        return {}
    out = load_company_profiles()
    for item in universe:
        t = item["ticker"]
        if t in out:  # 정적 데이터 — 재수집 안 함
            continue
        cc = codes.get(t)
        if not cc:
            continue
        prof = dart.company(cc)
        if prof:
            out[t] = prof
    if out:
        _write_json(COMPANY_PROFILES_FILE, out)
    return out


def load_company_profiles() -> dict[str, dict]:
    if not COMPANY_PROFILES_FILE.exists():
        return {}
    return json.loads(COMPANY_PROFILES_FILE.read_text(encoding="utf-8"))


def save_shortform_bg(data: bytes) -> None:
    """숏폼 카드 배경 이미지 원본 1장을 저장(업로드분). 서빙은 짧은 앱 URL로 → 장면 SVG에 data URI를
    박지 않아 DB가 커지지 않는다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SHORTFORM_BG_FILE.write_bytes(data)


def shortform_bg_path():
    return SHORTFORM_BG_FILE if SHORTFORM_BG_FILE.exists() else None


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


def fetch_kr_dividends(universe: list[dict] | None = None, bsns_year: str | None = None) -> int:
    """KR 주당 현금배당금(DART alotMatter) → fundamentals.json에 dps 병합. 무배당은 dps=None.
    연 결산배당이라 분기 1회 갱신(DART 재수집 시)이면 충분. 시도 종목 수 반환."""
    universe = universe if universe is not None else load_universe()
    bsns_year = bsns_year or str(datetime.date.today().year - 1)
    codes = dart.corp_codes()
    if not codes:
        return 0
    fund = load_fundamentals()
    n = 0
    for item in universe:
        t = item["ticker"]
        cc = codes.get(t)
        if not cc:
            continue
        fund.setdefault(t, {})["dps"] = dart.dividend(cc, bsns_year)  # None=무배당
        n += 1
    _write_json(FUNDAMENTALS_FILE, fund)
    return n


def kr_dividends(prices: dict[str, list[float]] | None = None) -> dict[str, dict]:
    """KR 배당주 — {ticker: {dps(주당 연배당,원), div_yield(%), price, div_months}}. 배당 있는 종목만.
    ⚠️ 시세가 스케일 상태면 div_yield·price는 왜곡(연배당 income=dps×주수는 DART라 정확). 지급월은 결산배당
    익년 4월 근사([4])."""
    fund = load_fundamentals()
    if not fund:
        return {}
    prices = prices if prices is not None else load_price_series()
    out = {}
    for t, f in fund.items():
        dps = f.get("dps")
        if not dps or dps <= 0:
            continue
        closes = prices.get(t)
        price = float(closes[-1]) if closes else None
        out[t] = {"dps": round(float(dps), 2), "price": round(price) if price else None,
                  "div_yield": round(dps / price * 100, 2) if price else None, "div_months": [4]}
    return out


def load_fundamentals_history() -> dict[str, dict]:
    if not FUNDAMENTALS_HISTORY_FILE.exists():
        return {}
    return json.loads(FUNDAMENTALS_HISTORY_FILE.read_text(encoding="utf-8"))


def compute_quality() -> int:
    """당해 재무(fundamentals) + 전년(fundamentals_history)으로 축약 F-Score를 계산해 fundamentals.json에
    quality로 저장(엔진 퀄리티 팩터가 읽음). 당해=전년도(year-1) 기준이라 직전 비교연도는 year-2."""
    from signal_desk.signals import quality
    fund = load_fundamentals()
    if not fund:
        return 0
    hist = load_fundamentals_history()
    prev_year = str(datetime.date.today().year - 2)
    n = 0
    for t, m in fund.items():
        prev = (hist.get(t) or {}).get(prev_year) or {}
        m["quality"] = quality.evaluate(m, prev)
        n += 1
    _write_json(FUNDAMENTALS_FILE, fund)
    return n


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


def load_us_earnings_calendar() -> dict[str, str]:
    """미국 실적발표 예정일 캐시 {ticker: 'YYYY-MM-DD'}. 메타(_fetched)는 제외하고 반환."""
    if not US_EARNINGS_FILE.exists():
        return {}
    data = json.loads(US_EARNINGS_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def fetch_us_earnings_calendar(ttl_days: int = 1) -> int:
    """Alpha Vantage EARNINGS_CALENDAR(벌크 1콜)로 미국 실적 예정일 갱신. 하루 1회만(TTL) —
    무료 티어 25콜 절약. 갱신했으면 종목 수, 스킵(신선)이면 -1, 실패면 0."""
    from datetime import date
    if US_EARNINGS_FILE.exists():
        try:
            meta = json.loads(US_EARNINGS_FILE.read_text(encoding="utf-8")).get("_fetched")
            if meta and (date.today() - date.fromisoformat(str(meta))).days < ttl_days:
                return -1  # 아직 신선 → AV 콜 생략
        except (ValueError, json.JSONDecodeError):
            pass
    from signal_desk.ingest import alphavantage
    cal = alphavantage.earnings_calendar("3month")
    if not cal:
        return 0
    _write_json(US_EARNINGS_FILE, {**cal, "_fetched": date.today().isoformat()})
    return len(cal)


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
        if "dps" in cur:  # 이 버전으로 이미 수집됨(dps 키 존재 = 배당 포함 백필 완료)
            continue
        f = edgar.fundamentals(t)
        done += 1  # 호출 시도 카운트(스로틀)
        if not f:
            continue
        cache.setdefault(t, {"shares": None, "per": None, "sector": None})
        cache[t]["net_income"] = f.get("net_income")
        cache[t]["equity"] = f.get("equity")
        cache[t]["dps"] = f.get("dps")  # 주당 연배당(배당 플래너·수익률용)
        cache[t]["div_months"] = f.get("div_months") or []  # 추정 배당 지급월(캘린더용)
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


def us_dividends(prices: dict[str, list[float]] | None = None) -> dict[str, dict]:
    """US 배당주 — {ticker: {dps(주당 연배당), div_yield(%), price}}. 배당 있는 종목만(dps>0).
    EDGAR TTM 주당배당 + 최신 종가로 수익률 계산(배당 플래너용)."""
    fund = load_us_fundamentals()
    if not fund:
        return {}
    prices = prices if prices is not None else load_us_price_series()
    out = {}
    for t, f in fund.items():
        dps = f.get("dps")
        if not dps or dps <= 0:
            continue
        closes = prices.get(t)
        price = float(closes[-1]) if closes else None
        out[t] = {"dps": round(float(dps), 4), "price": round(price, 2) if price else None,
                  "div_yield": round(dps / price * 100, 2) if price else None,
                  "div_months": f.get("div_months") or []}
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


def _json_rows(path) -> int | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return len(d) if isinstance(d, (list, dict)) else None
    except Exception:
        return None


def data_freshness() -> list[dict]:
    """데이터 소스별 최종 갱신 시각·경과·행수·stale 여부(캐시 파일 mtime 기준). 관리자 신선도 대시보드용.
    stale_days 초과면 stale=True(소스별 갱신 주기에 맞춘 임계)."""
    now = datetime.datetime.now().timestamp()

    def e(key, label, path, stale_days, rows=None):
        if not path.exists():
            return {"key": key, "label": label, "updated": None, "age_hours": None,
                    "rows": rows, "stale": True}
        mt = path.stat().st_mtime
        age_h = (now - mt) / 3600
        return {"key": key, "label": label,
                "updated": datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
                "age_hours": round(age_h, 1), "rows": rows, "stale": age_h > stale_days * 24}

    return [
        e("prices", "국내 시세", PRICES_FILE, 2),
        e("us_prices", "미국 시세", US_PRICES_FILE, 2),
        e("fundamentals", "재무(DART)", FUNDAMENTALS_FILE, 100, _json_rows(FUNDAMENTALS_FILE)),
        e("flows", "종목 수급(네이버)", FLOWS_FILE, 2, _json_rows(FLOWS_FILE)),
        e("short", "공매도 비중(KRX)", SHORT_FILE, 2, _json_rows(SHORT_FILE)),
        e("consensus", "컨센서스 축적(네이버)", CONSENSUS_HISTORY_FILE, 2),
        e("market_flow", "시장 수급(토스)", MARKET_FLOW_FILE, 2),
        e("macro", "거시(FRED)", MACRO_FILE, 8, _json_rows(MACRO_FILE)),
        e("macro_kr", "거시(ECOS)", MACRO_KR_FILE, 8, _json_rows(MACRO_KR_FILE)),
        e("company", "기업개황(DART)", COMPANY_PROFILES_FILE, 365, _json_rows(COMPANY_PROFILES_FILE)),
        e("warnings", "투자경고(토스)", WARNINGS_FILE, 2, _json_rows(WARNINGS_FILE)),
        e("gurus", "거장 13F", GURUS_FILE, 40),
        e("us_fund", "미국 재무(EDGAR)", US_FUNDAMENTALS_FILE, 100, _json_rows(US_FUNDAMENTALS_FILE)),
        e("signal_hist", "시그널 히스토리(PIT)", SIGNAL_HISTORY_FILE, 2),
    ]


def snapshot_signals(signals, date: str | None = None) -> int:
    """오늘의 종목별 시그널·팩터값을 point-in-time으로 기록(일 1회). 수급·퀄리티·정성은 과거 PIT
    데이터가 없어 사전 백테스트가 불가했는데, 오늘부터 쌓아 향후 팩터 백테스트를 가능하게 한다.
    같은 날 재실행 시 그 날짜를 덮어쓴다. 반환: 기록한 종목 수."""
    if not signals:
        return 0
    date = date or datetime.date.today().isoformat()
    rows = [{"date": date, "ticker": s.ticker, "score": round(s.score, 3), "kind": s.kind,
             "technical": round(s.technical_score, 3), "fundamental": round(s.fundamental_score, 3),
             "valuation": s.valuation_percentile, "reversion": round(s.reversion_score, 3),
             "qualitative": s.qualitative_score, "flow": s.flow_intensity,
             "quality": s.quality_points, "momentum": s.momentum_ret} for s in signals]
    df_new = pd.DataFrame(rows)
    if SIGNAL_HISTORY_FILE.exists():
        old = pd.read_parquet(SIGNAL_HISTORY_FILE)
        old = old[old["date"] != date]  # 같은 날 재실행 → 갱신
        df_new = pd.concat([old, df_new], ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df_new.to_parquet(SIGNAL_HISTORY_FILE, index=False)
    return len(rows)


def load_signal_history():
    if not SIGNAL_HISTORY_FILE.exists():
        return pd.DataFrame()
    return pd.read_parquet(SIGNAL_HISTORY_FILE)


def load_all_dated_closes() -> dict[str, tuple[list[str], list[float]]]:
    """ticker -> (dates[], closes[]) 오래된→최신, 국내+미국 통합. 실측 성과(accuracy) 조인용.
    각 parquet을 1회만 읽어 종목별 (날짜, 종가) 짝을 만든다(실시간 잠정봉은 제외 — 성숙 판정 왜곡 방지)."""
    out: dict[str, tuple[list[str], list[float]]] = {}
    for f in (PRICES_FILE, US_PRICES_FILE):
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        if df.empty:
            continue
        df = df.sort_values(["ticker", "date"])
        for t, g in df.groupby("ticker"):
            out[str(t)] = ([str(d) for d in g["date"].tolist()],
                           [float(c) for c in g["close"].tolist()])
    return out


def signal_history_for(ticker: str) -> dict[str, dict]:
    """종목별 실측 시그널 이력 {date: {kind, score}} — 차트 구간을 '실측 우선(없으면 가격기반 재현)'
    으로 그리는 데 사용. PIT 스냅샷을 쌓기 시작한 날짜부터만 존재."""
    df = load_signal_history()
    if df.empty or "ticker" not in df.columns:
        return {}
    sub = df[df["ticker"] == ticker]
    out = {}
    for _, r in sub.iterrows():
        try:
            out[str(r["date"])] = {"kind": str(r["kind"]), "score": float(r["score"])}
        except (TypeError, ValueError):
            continue
    return out


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
