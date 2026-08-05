"""캐시 로더 — apt-signal 컨벤션(parquet=시계열, json=메타/소형 데이터)을 그대로 따른다.

ingest 모듈은 데이터만 반환하고, 캐시 형식·경로 결정은 전부 이 파일이 담당한다.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd

from signal_desk.ingest import dart, fred, krx, krx_open_api

try:  # 손상 parquet(잘린 파일) 판별용 — 없으면 빈 튜플로 폴백
    from pyarrow.lib import ArrowInvalid as _ArrowInvalid
except Exception:  # pragma: no cover
    _ArrowInvalid = ()

_pd_read_parquet = pd.read_parquet  # 원본 참조(_read_parquet 재귀 방지용)

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
US_SYMBOLS_FILE = CACHE_DIR / "us_symbols.json"     # {provider: {ticker: 실제로 통한 심볼 표기}}
US_PRICE_SKIP_FILE = CACHE_DIR / "us_price_skip.json"  # {ticker: {fails, last}} — 자동 백필 유예
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
HARNESS_LAST_FILE = CACHE_DIR / "harness_last.json"  # 마지막 sigdesk harness 결과 — 시그널 판별력 A열

PRICE_HISTORY_DAYS = 1825  # 약 5년 — 모멘텀(60일 최강)·다중국면 팩터/백테스트 신뢰도. 최초 1회 전량, 이후 증분
US_SKIP_AFTER_FAILS = 3    # 이만큼 연속 실패하면 자동 백필에서 잠시 빼둔다(수동 갱신은 무시)
US_SKIP_DAYS = 7           # 유예 기간 — 상장·표기 변경이 반영될 만한 간격
US_STALE_DAYS = 3          # 마지막 일봉이 이보다 오래되면 갱신 대상(금→월 주말 포함)


def _write_json(path: Path, data) -> None:
    """원자적 JSON 쓰기 — 임시파일에 쓴 뒤 os.replace로 교체(쓰기 중 프로세스 종료 시 손상 방지)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """원자적 parquet 쓰기 — 임시파일에 쓴 뒤 os.replace로 교체. 쓰기 도중 프로세스가 죽어도
    기존 파일이 잘리지 않는다(재시작 반복 시 parquet footer 손상 방지)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_parquet(path: Path) -> pd.DataFrame:
    """손상에 강한 parquet 읽기 — 잘린/깨진 파일(재시작 중 쓰기 중단 등)이면 폐기하고 빈 DF 반환한다.
    일일 수집·증분 백필이 다시 채우므로, 캐시 파일 하나가 앱 전체(요청·봇 루프)를 죽이지 않는다."""
    try:
        return _pd_read_parquet(path)
    except (_ArrowInvalid, OSError) as e:
        log.warning("parquet 손상/읽기 실패 — 폐기 후 재생성 대기: %s (%s)", path.name, type(e).__name__)
        try:
            path.unlink()
        except OSError:
            pass
        return pd.DataFrame()


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
    existing = _read_parquet(PRICES_FILE) if PRICES_FILE.exists() else None
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
    _write_parquet(combined, PRICES_FILE)
    clear_kr_price_cache()
    return combined


def prices_depth_days() -> int:
    """저장된 일봉의 최대 이력 길이(달력일). 가장 깊은 종목 기준 — 신규 상장 종목이 섞여도
    '얼마나 과거까지 받아봤나'를 그대로 보여준다. 캐시가 없으면 0."""
    if not PRICES_FILE.exists():
        return 0
    df = _read_parquet(PRICES_FILE)
    if df.empty:
        return 0
    oldest = datetime.date.fromisoformat(str(df["date"].min())[:10])
    return (datetime.date.today() - oldest).days


def prices_need_deep_backfill(days: int = PRICE_HISTORY_DAYS) -> bool:
    """목표 이력(days)에 크게 못 미치면 True → 다음 수집에서 전량 백필.

    불리언 플래그(prices_deep_backfilled)로 한 번만 백필하던 방식은 목표 깊이를 나중에 올려도
    래치가 걸린 채 얕은 이력으로 영구히 남는다(실제로 400일치만 쌓인 채 5년으로 올라간 이력 있음).
    실제 커버리지를 재면 목표를 올린 다음 수집에서 스스로 다시 채운다."""
    return prices_depth_days() < int(days * 0.8)


def fetch_flows(universe: list[dict] | None = None, days: int = 20, time_budget: float = 30.0) -> dict:
    """최근 days 거래일 투자자별 순매수(외국인·기관, KR)를 종목별로 수집 → flows.json.
    intensity = (외국인+기관 순매수) / 전체 거래량 — 종목 규모 무관하게 [-1,1]로 자기정규화(수급 강도).
    소스: 네이버 금융(pykrx 투자자 엔드포인트가 KRX 스키마 변경으로 죽어 대체). 실패분 건너뜀.

    배포 환경에서 네이버가 간헐 타임아웃(종목당 10초)을 내면 200종목을 다 두드리다 요청이
    통째로 시간 초과된다 → time_budget(초) 내로 제한하고, 기존 flows.json에 누적 병합해
    '아직 없는 종목 먼저' 순서로 여러 번 갱신에 걸쳐 전량을 채운다(US 시세 백필과 동일 패턴)."""
    from signal_desk.ingest import naver
    universe = universe if universe is not None else load_universe()
    out: dict[str, dict] = load_flows()  # 기존 커버리지에 누적(부분 수집이 반복 갱신으로 채워지도록)
    ordered = sorted(universe, key=lambda it: it["ticker"] in out)  # 미수집 종목 먼저
    start = time.monotonic()
    consec = 0  # 연속 실패
    got = 0     # 이번 실행 신규 성공 수
    for item in ordered:
        if time.monotonic() - start > time_budget:
            log.info("수급 수집 시간예산(%.0fs) 도달 — 부분 저장(%d종목), 다음 갱신에서 계속.", time_budget, len(out))
            break
        ticker = item["ticker"]
        fl = naver.investor_flow(ticker, days)
        if not fl:
            consec += 1
            # 소스가 통째로 막힘(IP 차단 등): 신규 성공 0인데 연속 실패 누적 → 조기 중단(다른 팩터 영향 없음).
            if got == 0 and consec >= 8:
                log.warning("수급 수집 중단 — 네이버 투자자 수급 응답 없음(%d연속). "
                            "수급 팩터는 데이터 없음으로 자동 제외됩니다(다른 팩터엔 영향 없음).", consec)
                break
            # 중간에 소스가 불안정해진 경우도 조기 중단(이미 모은 분은 유지).
            if consec >= 25:
                log.warning("수급 수집 중단 — 네이버 연속 실패 %d(소스 불안정). 수집분(%d종목) 유지.", consec, len(out))
                break
            continue
        consec = 0
        got += 1
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
    df = _read_parquet(PRICES_FILE)
    if df.empty or "volume" not in df.columns:
        return {}
    out: dict[str, dict[str, float]] = {}
    for t, g in df.groupby("ticker"):
        out[str(t)] = {str(d): float(v) for d, v in zip(g["date"], g["volume"])}
    return out


def fetch_short(universe: list[dict] | None = None, days: int = 20, time_budget: float = 30.0) -> dict:
    """최근 days 거래일 종목별 공매도 거래비중(KR)을 수집 → short.json.
    short_ratio = Σ공매도거래량 / Σ총거래량 (둘 다 주수 → 종목 규모·스케일 시세 무관).
    소스: KRX 외부용 엔드포인트(ingest.krx_short). 공매도량은 KRX, 총거래량은 우리 OHLCV(동일 KRX 원천).

    fetch_flows와 동일: time_budget(초) 내로 제한 + 기존 short.json에 누적 병합(미수집 종목 먼저) →
    소스가 느려도 요청이 통째로 시간 초과되지 않고, 여러 번 갱신에 걸쳐 전량을 채운다."""
    from signal_desk.ingest import krx_short
    universe = universe if universe is not None else load_universe()
    vol_by = _volume_by_ticker_date()
    out: dict[str, dict] = load_short()  # 기존 커버리지에 누적
    ordered = sorted(universe, key=lambda it: it["ticker"] in out)  # 미수집 종목 먼저
    start = time.monotonic()
    consec = 0  # 연속 실패
    got = 0     # 이번 실행 신규 성공
    for item in ordered:
        if time.monotonic() - start > time_budget:
            log.info("공매도 수집 시간예산(%.0fs) 도달 — 부분 저장(%d종목), 다음 갱신에서 계속.", time_budget, len(out))
            break
        ticker = item["ticker"]
        sv = krx_short.short_volume(ticker, days)
        if not sv:
            consec += 1
            # 소스가 통째로 막히면(스키마 변경 등) 조기 중단. 공매도 팩터는 자동 제외.
            if got == 0 and consec >= 8:
                log.warning("공매도 수집 중단 — KRX 응답 없음(%d연속). "
                            "공매도 팩터는 데이터 없음으로 자동 제외됩니다(다른 팩터엔 영향 없음).", consec)
                break
            if consec >= 25:
                log.warning("공매도 수집 중단 — KRX 연속 실패 %d(소스 불안정). 수집분(%d종목) 유지.", consec, len(out))
                break
            continue
        consec = 0
        got += 1
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
        old = _read_parquet(CONSENSUS_HISTORY_FILE)
        if not old.empty and "date" in old.columns:
            old = old[old["date"] != date]  # 같은 날 재실행 → 갱신
            df_new = pd.concat([old, df_new], ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_parquet(df_new, CONSENSUS_HISTORY_FILE)
    return len(rows)


def load_consensus_history():
    if not CONSENSUS_HISTORY_FILE.exists():
        return pd.DataFrame()
    return _read_parquet(CONSENSUS_HISTORY_FILE)


def load_consensus_latest() -> dict[str, dict]:
    """종목별 가장 최근 컨센서스 스냅샷 {ticker: row}. (목표가 v2 등에서 '현재 수준'이 필요할 때용)"""
    df = load_consensus_history()
    if df.empty:
        return {}
    latest = df.sort_values("date").groupby("ticker").tail(1)
    return {r["ticker"]: {k: r[k] for k in df.columns} for _, r in latest.iterrows()}


def kr_engine_inputs() -> dict:
    """국내 evaluate에 넣는 데이터 입력 한 벌(config 제외) — UI와 봇이 같은 함수를 쓰게 한다.

    한쪽에만 팩터가 빠지면 화면의 '매수 후보'와 봇이 실제로 사는 종목이 갈라지는데, 그 차이는
    어느 화면에도 안 나타난다(공매도가 봇에만 빠져 있었다). 새 팩터 입력은 여기에만 추가한다."""
    from signal_desk import kb
    return {"sentiment": kb.sentiment_map(), "flows": load_flows(), "shorts": load_short()}


def _market_dates() -> list[str]:
    """국내 거래일 목록(오래된→최신) — 성숙 판정용. 실시간 잠정봉은 포함하지 않는다."""
    if not PRICES_FILE.exists():
        return []
    df = _read_parquet(PRICES_FILE)
    if df.empty or "date" not in df.columns:
        return []
    return sorted({str(d) for d in df["date"].tolist()})


# 컨센서스 리비전(Δ목표주가·Δ선행EPS)을 '측정'할 수 있게 되는 조건. 날짜 단위 IC 관측 수이고
# 종목 표본 수가 아니다 — 같은 날 200종목은 하나의 관측에 가깝다(횡단면이 서로 독립이 아니다).
REVISION_MIN_TESTABLE_DATES = 20
REVISION_HORIZON = 20


def consensus_readiness(*, horizon: int = REVISION_HORIZON,
                        need: int = REVISION_MIN_TESTABLE_DATES) -> dict:
    """컨센서스 축적이 언제 '판정 가능'해지는지 — 축적만 하는 데이터에 판정 날짜를 붙인다.

    조건이 없는 축적은 영원히 안 본다. 여기서 재는 것은 **측정 가능 시점**이지 반영 시점이 아니다
    (판별력이 판정 불가인 동안 가중치·부호를 만지지 않는다는 원칙은 그대로다).

    testable = Δ를 계산할 이전 스냅샷이 있고, 이후 horizon 거래일 종가까지 존재하는 날짜.
    """
    df = load_consensus_history()
    if df.empty or "date" not in df.columns:
        return {"days": 0, "tickers": 0, "testable_dates": 0, "need": need, "horizon": horizon,
                "ready": False, "blocked_reason": "컨센서스 스냅샷 없음(평일 마감 후 누적)",
                "eta_trading_days": need + horizon, "eta_date": None}
    dates = sorted({str(d) for d in df["date"].tolist()})
    mkt = _market_dates()
    testable = 0
    if mkt:
        for cd in dates[1:]:                       # 첫 날짜는 Δ 계산 불가
            nxt = next((k for k, d in enumerate(mkt) if d > cd), None)
            if nxt is not None and nxt + horizon <= len(mkt) - 1:
                testable += 1
    future_needed = max(0, need - len(dates))
    eta_days = 0 if testable >= need else future_needed + horizon
    eta_date = None
    if eta_days:
        d, left = datetime.date.today(), eta_days
        while left > 0:                            # 휴일 미반영 추정(영업일 기준)
            d += datetime.timedelta(days=1)
            if d.weekday() < 5:
                left -= 1
        eta_date = d.isoformat()
    return {"days": len(dates), "tickers": int(df["ticker"].nunique()),
            "first_date": dates[0], "last_date": dates[-1],
            "testable_dates": testable, "need": need, "horizon": horizon,
            "ready": testable >= need,
            "blocked_reason": None if testable >= need
            else f"측정 가능 날짜 {testable}/{need} — Δ 계산 후 {horizon}거래일 성숙 대기",
            "eta_trading_days": eta_days, "eta_date": eta_date,
            "note": "측정 가능 시점이지 반영 시점이 아니다. 조건 충족 시 리비전 팩터 IC를 재고, "
                    "판별력이 확인되기 전에는 점수에 넣지 않는다."}


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
    # 퀄리티(축약 F-Score)는 재무의 **파생값**이다 — 여기서 같이 채운다.
    # 2026-08-05 진단: `compute_quality()`의 호출처가 관리자 수동 refresh 하나뿐이었고(분기 1회 조건),
    # `sigdesk fetch`(CLI)는 이 함수만 불렀다. 이 함수가 dict를 새로 써서 저장하므로 **CLI로 갱신할
    # 때마다 quality가 지워졌다** — 실측 `quality.has=True 0/198`, 가중 0.15가 통째로 미발동.
    # 파생값을 원본 쓰는 함수 밖에 두면 어느 호출자든 잊을 수 있다. 안으로 넣어 잊을 수 없게 한다.
    _attach_quality(out)
    _write_json(FUNDAMENTALS_FILE, out)
    return out


def _attach_quality(fund: dict) -> int:
    """`fund`에 축약 F-Score를 붙인다(제자리 수정). 반환: 계산된 종목 수.

    이력이 없으면 조용히 넘기지 않고 이유를 로그로 남긴다 — 가중 0.15가 미발동인데 화면에
    "커버리지 0%"만 뜨면 '이력 부족'과 '배선 누락'을 구분할 수 없다(실제로 못 했다).
    """
    from signal_desk.signals import quality

    hist = load_fundamentals_history()
    if not hist:
        log.warning("퀄리티 미계산 — fundamentals_history 없음(가중 %s 미발동). "
                    "`fetch_fundamentals_history` 를 먼저 돌려야 한다", "weight_quality")
        return 0
    prev_year = str(datetime.date.today().year - 2)
    n = 0
    for t, m in fund.items():
        if not isinstance(m, dict):
            continue
        m["quality"] = quality.evaluate(m, (hist.get(t) or {}).get(prev_year) or {})
        if m["quality"].get("has"):
            n += 1
    if not n:
        log.warning("퀄리티 계산했으나 has=True 0건 — 전년(%s) 재무가 비었는지 확인", prev_year)
    return n


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
    """캐시된 재무에 축약 F-Score를 다시 붙인다(관리자 수동 경로용).

    평소에는 `fetch_fundamentals`가 안에서 같이 채우므로 부를 필요가 없다 — 이 함수만 있고
    호출을 잊는 구조였던 것이 2026-08-05 진단에서 발견된 버그다(`_attach_quality` 주석 참고).
    """
    fund = load_fundamentals()
    if not fund:
        return 0
    n = _attach_quality(fund)
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


def _load_json_dict(path) -> dict:
    if not path.exists():
        return {}
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return out if isinstance(out, dict) else {}


def _symbol_candidates(ticker: str, resolved: dict) -> list[str]:
    """이 티커로 외부에 물어볼 표기 목록 — 통한 적 있으면 그것만, 없으면 후보 전부."""
    from signal_desk.ingest import us
    known = resolved.get(ticker)
    return [known] if known else us.symbol_variants(ticker)


def us_price_deferred(ticker: str, skip: dict | None = None) -> bool:
    """반복 실패로 자동 백필에서 유예 중인 티커인가.

    유예가 없으면 어떤 표기로도 받을 수 없는 종목을 30분마다 영원히 재시도하며 로그를 채운다
    (실측: BRK-B·BF-B가 루프마다 토스 404 + KIS 3거래소 탐지를 반복). 관리자 '데이터 갱신'은
    이 유예를 무시하고 다시 시도한다 — 사람이 명시적으로 요청한 것이므로."""
    rec = (skip if skip is not None else _load_json_dict(US_PRICE_SKIP_FILE)).get(ticker)
    if not isinstance(rec, dict) or int(rec.get("fails") or 0) < US_SKIP_AFTER_FAILS:
        return False
    try:
        last = datetime.date.fromisoformat(str(rec.get("last"))[:10])
    except ValueError:
        return False
    return (datetime.date.today() - last).days < US_SKIP_DAYS


def us_price_skips() -> dict:
    """{ticker: {fails, last}} — 유예 판정을 티커마다 파일을 다시 읽지 않고 하도록 한 번에 준다."""
    return _load_json_dict(US_PRICE_SKIP_FILE)


def us_price_deferred_tickers() -> list[str]:
    """자동 백필에서 유예 중인 티커 목록 — 조용히 빠진 종목을 하루 한 번 드러내기 위한 것."""
    skip = us_price_skips()
    return sorted(t for t in skip if us_price_deferred(t, skip))


def us_price_last_dates() -> dict[str, str]:
    """ticker → 마지막 일봉 날짜(YYYY-MM-DD). 캐시 없으면 빈 dict."""
    if not US_PRICES_FILE.exists():
        return {}
    df = _read_parquet(US_PRICES_FILE)
    if df.empty or "ticker" not in df.columns or "date" not in df.columns:
        return {}
    return {str(t): str(d)[:10] for t, d in df.groupby("ticker")["date"].max().items()}


def us_prices_stale_tickers(tickers: list[str] | None = None, *,
                            max_age_days: int = US_STALE_DAYS,
                            as_of: datetime.date | None = None) -> list[str]:
    """마지막 일봉이 cutoff보다 오래된(또는 없는) 티커. 유예 여부는 호출측에서 거른다.

    누락 백필(`_backfill_us_prices_batch`)과 역할이 다르다 — 여기는 '이미 있던 종목이
    며칠째 안 움직인' 경우를 잡는다. max_age_days=3이면 금→월 주말에도 금요일 종가는 신선."""
    as_of = as_of or datetime.date.today()
    cutoff = (as_of - datetime.timedelta(days=int(max_age_days))).isoformat()
    last = us_price_last_dates()
    universe = tickers if tickers is not None else [u["ticker"] for u in load_us_universe()]
    return [t for t in universe if str(last.get(t) or "")[:10] < cutoff]


def fetch_us_prices(tickers: list[str], days: int = 400) -> int:
    """지정 티커들의 미국 일봉을 수집해 us_prices.parquet에 병합(upsert). 반환: 성공 종목 수.

    토스 우선, 실패 시 KIS 해외로 폴백. 클래스주(BRK-B)는 제공자가 받는 표기가 달라 후보를
    순서대로 시도하고(`us.symbol_variants`) 통한 표기를 us_symbols.json에 캐시한다. 거래소코드
    (EXCD)도 탐지 결과를 us_exchanges.json에 캐시해 재탐지를 피한다. 어느 표기로도 실패하면
    연속 실패 횟수를 남겨(us_price_skip.json) 자동 백필이 무한 재시도하지 않게 한다.

    (ticker, date) 단위로 keep='last' 병합한다 — 짧은 days로 일일 갱신해도 과거 이력이 지워지지
    않는다(예전엔 요청 종목 행을 통째로 갈아끼워 days=60 갱신이 5년치를 날렸다)."""
    from signal_desk.ingest import toss, us
    use_toss = toss.available()  # 토스 우선(KR+US 단일·표준443·안정) → 미설정 시 KIS 폴백
    exch = _load_us_exchanges()
    syms = _load_json_dict(US_SYMBOLS_FILE)
    skip = _load_json_dict(US_PRICE_SKIP_FILE)
    existing = _read_parquet(US_PRICES_FILE) if US_PRICES_FILE.exists() else pd.DataFrame()
    rows: list[dict] = []
    ok = 0
    for t in tickers:
        bars: list[dict] | None = None
        for sym in (_symbol_candidates(t, syms.get("toss", {})) if use_toss else []):
            bars = toss.daily_ohlcv(sym, count=min(days, 200))
            if bars:
                syms.setdefault("toss", {})[t] = sym
                break
        if not bars:  # 토스 미설정·실패 시 KIS 해외로 폴백
            for sym in _symbol_candidates(t, syms.get("kis", {})):
                excd = exch.get(t) or us.detect_exchange(sym)
                if not excd:
                    continue
                bars = us.us_ohlcv(sym, days=days, excd=excd)
                if bars:
                    exch[t], syms.setdefault("kis", {})[t] = excd, sym
                    break
        if not bars:
            rec = skip.get(t) if isinstance(skip.get(t), dict) else {}
            skip[t] = {"fails": int(rec.get("fails") or 0) + 1,
                       "last": datetime.date.today().isoformat()}
            log.warning("US 시세 수집 실패, 제외: %s (표기 %s 전부 실패, 연속 %d회)",
                        t, "/".join(us.symbol_variants(t)), skip[t]["fails"])
            continue
        skip.pop(t, None)
        rows.extend({"ticker": t, **b} for b in bars)
        ok += 1
    if rows:
        new = pd.DataFrame(rows)
        combined = pd.concat([existing, new], ignore_index=True) if not existing.empty else new
        combined = (combined.drop_duplicates(subset=["ticker", "date"], keep="last")
                    .sort_values(["ticker", "date"]).reset_index(drop=True)
                    [["date", "ticker", "open", "close", "volume"]])
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _write_parquet(combined, US_PRICES_FILE)
        clear_us_price_cache()
    _write_json(US_EXCHANGES_FILE, exch)
    _write_json(US_SYMBOLS_FILE, syms)
    _write_json(US_PRICE_SKIP_FILE, skip)
    return ok


# US 시세 프로세스 캐시 — parquet을 요청마다 여러 번 읽으면(시그널+quotes+차트) Railway 저메모리에서
# OOM spike가 난다. mtime이 같으면 파생 dict만 재사용하고, 실시간 오버레이는 호출 시점에 얹는다.
_us_px_cache: dict = {"mtime": None, "series": {}, "quotes": {}, "dates": {}}


def clear_us_price_cache() -> None:
    """테스트·강제 무효화용. 일반 경로는 파일 mtime 변경으로 자동 무효화."""
    _us_px_cache["mtime"] = None
    _us_px_cache["series"] = {}
    _us_px_cache["quotes"] = {}
    _us_px_cache["dates"] = {}


def _us_prices_raw() -> tuple[dict[str, list[float]], dict[str, dict], dict[str, list[str]]]:
    """us_prices.parquet 1회 읽어 (종가열, 거래량요약, 날짜열)을 돌려준다. mtime 캐시."""
    if not US_PRICES_FILE.exists():
        clear_us_price_cache()
        return {}, {}, {}
    mtime = US_PRICES_FILE.stat().st_mtime
    if _us_px_cache["mtime"] == mtime and _us_px_cache["series"] is not None:
        return _us_px_cache["series"], _us_px_cache["quotes"], _us_px_cache["dates"]
    df = _read_parquet(US_PRICES_FILE)
    if df.empty:
        clear_us_price_cache()
        _us_px_cache["mtime"] = mtime
        return {}, {}, {}
    df = df.sort_values(["ticker", "date"])
    has_vol = "volume" in df.columns
    series: dict[str, list[float]] = {}
    quotes: dict[str, dict] = {}
    dates: dict[str, list[str]] = {}
    for t, g in df.groupby("ticker"):
        key = str(t)
        series[key] = [float(c) for c in g["close"].tolist()]
        dates[key] = [str(d) for d in g["date"].tolist()]
        if has_vol:
            vols = [float(v) for v in g["volume"].tolist() if v == v]
            quotes[key] = {"vol": vols[-1] if vols else None,
                           "vol_avg": round(sum(vols[-20:]) / len(vols[-20:])) if vols else None}
    _us_px_cache["mtime"] = mtime
    _us_px_cache["series"] = series
    _us_px_cache["quotes"] = quotes
    _us_px_cache["dates"] = dates
    return series, quotes, dates


def load_us_price_bundle() -> tuple[dict[str, list[float]], dict[str, dict]]:
    """US 종가 시계열 + 거래량 요약을 한 번에(parquet 1회). 시그널 리스트 조립용."""
    series, quotes, _ = _us_prices_raw()
    return _overlay_closes(series), quotes


def load_us_price_series() -> dict[str, list[float]]:
    series, _, _ = _us_prices_raw()
    return _overlay_closes(series)


def load_us_dates_by_ticker() -> dict[str, list[str]]:
    """US ticker → 날짜 리스트(load_us_price_series와 길이 정합, 잠정봉 포함)."""
    _, _, dates = _us_prices_raw()
    if _LIVE_QUOTES:
        today = datetime.date.today().isoformat()
        dates = {t: (ds + [today]) if (_LIVE_QUOTES.get(t) and ds) else ds
                 for t, ds in dates.items()}
    return dates


def load_us_quotes() -> dict[str, dict]:
    """US 종목별 최신 거래량·20일 평균 거래량(정렬·표기용). 시총은 데이터 소스 없어 미제공."""
    _, quotes, _ = _us_prices_raw()
    return quotes


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
        cache[t] = {"shares": ov["shares"], "per": ov["per"], "sector": ov["sector"],
                    "industry": ov.get("industry"), "description": ov.get("description")}  # 사업 개요 요약용
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


def fetch_warnings(tickers: list[str], pause: float = 0.25) -> int:
    """토스 투자경고/거래정지/과열/VI를 종목별 조회해 warnings.json에 캐시(활성 유형만).
    매수 가드레일(veto)이 이 집합을 근거로 씀. 토스 미설정 시 0.

    warnings는 종목별 단건 API이고 STOCK 그룹 ~5 TPS라, 종목 사이 pause초를 둔다
    (배치 엔드포인트 없음). 요청 실패분은 기존 캐시를 유지해 부분 429가 전량 삭제로
    이어지지 않게 한다. 반환: 활성 경고가 있는 종목 수."""
    from signal_desk.ingest import toss
    if not toss.available():
        return 0
    out: dict = {}
    if WARNINGS_FILE.exists():
        try:
            prev = json.loads(WARNINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                out = prev
        except (json.JSONDecodeError, OSError):
            pass
    for i, t in enumerate(tickers):
        if i and pause > 0:
            time.sleep(pause)
        w = toss.warnings(t)
        if w is None:             # 요청 실패 — 기존 값 유지
            continue
        if w:
            out[t] = w
        else:
            out.pop(t, None)      # 정상·경고 해제
    _write_json(WARNINGS_FILE, out)
    return sum(1 for t in tickers if out.get(t))


def load_warned_tickers() -> set[str]:
    """활성 투자경고·거래정지 등이 걸린 종목 집합(매수 veto용). 없으면 빈 집합."""
    if not WARNINGS_FILE.exists():
        return set()
    return set(json.loads(WARNINGS_FILE.read_text(encoding="utf-8")).keys())


def warnings_status() -> dict:
    """투자경고 veto의 데이터 상태 — 경고 0종목이 '정상'인지 '미수집'인지 구분한다.
    veto 집합이 조용히 비면 매수 가드레일이 없는 것과 같으므로 이유를 붙여 노출한다."""
    from signal_desk.ingest import toss
    available = toss.available()
    if not WARNINGS_FILE.exists():
        return {"fetched": False, "active": 0, "updated": None, "toss_available": available,
                "blocked_reason": "미수집 — 토스 미설정" if not available else "미수집 — 아직 한 번도 안 받음"}
    return {"fetched": True, "active": len(load_warned_tickers()),
            "updated": datetime.datetime.fromtimestamp(
                WARNINGS_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "toss_available": available, "blocked_reason": None}


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
    """단일 종목 (date, close) — 프로세스 캐시에서 꺼내 전체 parquet 재읽기를 피한다."""
    series, _, dates = _us_prices_raw()
    closes = series.get(ticker)
    ds = dates.get(ticker)
    if not closes or not ds:
        return []
    return [{"date": d, "close": c} for d, c in zip(ds, closes)]


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


# KR 시세 프로세스 캐시 — 차트 클릭마다 2.7MB parquet + iterrows 하면 체감이 느리다.
# US와 같이 mtime이 같으면 파생 dict만 재사용하고, 실시간 오버레이는 호출 시점에 얹는다.
_kr_px_cache: dict = {"mtime": None, "series": {}, "dates": {}}


def clear_kr_price_cache() -> None:
    """테스트·강제 무효화용. 일반 경로는 파일 mtime 변경으로 자동 무효화."""
    _kr_px_cache["mtime"] = None
    _kr_px_cache["series"] = {}
    _kr_px_cache["dates"] = {}


def _kr_prices_raw() -> tuple[dict[str, list[float]], dict[str, list[str]]]:
    """prices.parquet 1회 읽어 (종가열, 날짜열). mtime 캐시."""
    if not PRICES_FILE.exists():
        clear_kr_price_cache()
        return {}, {}
    mtime = PRICES_FILE.stat().st_mtime
    if _kr_px_cache["mtime"] == mtime and _kr_px_cache["series"] is not None:
        return _kr_px_cache["series"], _kr_px_cache["dates"]
    df = _read_parquet(PRICES_FILE)
    if df.empty:
        clear_kr_price_cache()
        _kr_px_cache["mtime"] = mtime
        return {}, {}
    df = df.sort_values(["ticker", "date"])
    series: dict[str, list[float]] = {}
    dates: dict[str, list[str]] = {}
    for t, g in df.groupby("ticker"):
        key = str(t)
        series[key] = [float(c) for c in g["close"].tolist()]
        dates[key] = [str(d)[:10] for d in g["date"].tolist()]
    _kr_px_cache["mtime"] = mtime
    _kr_px_cache["series"] = series
    _kr_px_cache["dates"] = dates
    return series, dates


def load_price_series() -> dict[str, list[float]]:
    """ticker -> 종가 리스트(오래된→최신). engine.evaluate()/backtest_summary()에 바로 투입 가능.
    장중 실시간가가 설정돼 있으면 마지막에 잠정봉 1개를 얹는다(set_live_quotes)."""
    series, _ = _kr_prices_raw()
    return _overlay_closes(series)


def load_dates_by_ticker() -> dict[str, list[str]]:
    """ticker -> 날짜 리스트(오래된→최신) — load_price_series()와 동일 정렬. point-in-time 백테스트용."""
    _, dates = _kr_prices_raw()
    if _LIVE_QUOTES:  # load_price_series의 잠정봉과 길이 정합 유지(백테스트 date-close 짝 안 깨지게)
        today = datetime.date.today().isoformat()
        dates = {t: (ds + [today]) if (_LIVE_QUOTES.get(t) and ds) else ds for t, ds in dates.items()}
    return dates


def load_price_history(ticker: str) -> list[dict]:
    """단일 종목의 (date, close) 시계열(오래된→최신) — 차트용, 날짜를 유지한다.
    프로세스 캐시에서 꺼내 전체 parquet 재읽기·iterrows를 피한다.
    장중 잠정봉은 올리지 않는다(시그널 점수 축과 달리 차트는 확정 종가 기준)."""
    series, dates = _kr_prices_raw()
    closes = series.get(ticker)
    ds = dates.get(ticker)
    if not closes or not ds:
        return []
    return [{"date": d, "close": c} for d, c in zip(ds, closes)]


def load_index_history() -> list[dict]:
    """유니버스 종가로 만든 동일가중 정규화 지수(코스피200 근사) — [{date, close}].

    코스피 종합지수 원본 API가 없어, 전 구간 존재하는 종목들을 시작일 100으로 정규화해
    평균낸 동일가중 지수로 근사한다(시장 전체 흐름 참고용). 정확한 지수가 필요하면
    data.krx.co.kr 지수 데이터로 교체.
    """
    if not PRICES_FILE.exists():
        return []
    df = _read_parquet(PRICES_FILE)
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
    df = _read_parquet(PRICES_FILE)
    if df.empty:
        return {}
    has_vol = "volume" in df.columns
    fundamentals = load_fundamentals()
    df = df.sort_values(["ticker", "date"])
    out: dict[str, dict] = {}
    for ticker, g in df.groupby("ticker"):
        # parquet 결측이 float('nan')으로 오면 JSON 직렬화가 깨진다 — 유한값만 쓴다
        closes = [float(c) for c in g["close"].tolist() if c == c]
        if not closes:
            continue
        live = _LIVE_QUOTES.get(ticker)
        try:
            live_f = float(live) if live is not None and live == live else None
        except (TypeError, ValueError):
            live_f = None
        # 장중 실시간가가 있으면 현재가=live, 전일=마지막 종가
        price = live_f if live_f is not None and live_f > 0 else closes[-1]
        prev = closes[-1] if live_f is not None and live_f > 0 else (
            closes[-2] if len(closes) > 1 else price)
        if not (price > 0 and prev > 0):
            continue
        vol = vol_avg = None
        if has_vol:
            vols = [float(v) for v in g["volume"].tolist() if v == v]  # NaN 제외
            if vols:
                vol = vols[-1]
                vol_avg = round(sum(vols[-vol_window:]) / len(vols[-vol_window:]), 1)
        mcap = (fundamentals.get(ticker) or {}).get("mktcap")
        try:
            mcap = float(mcap) if mcap is not None and mcap == mcap else None
        except (TypeError, ValueError):
            mcap = None
        out[ticker] = {
            "price": round(price, 2),
            "prev_close": round(prev, 2),
            "change_pct": round((price / prev - 1) * 100, 2),
            "mktcap": mcap,
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
        # 컨센서스·시그널 PIT는 평일 마감후에만 쌓인다. stale_days=2면 금→일만 돼도 "오래됨"이
        # 뜬다(고장 아님). 주말·공휴일 버퍼로 4일.
        e("consensus", "컨센서스 축적(네이버)", CONSENSUS_HISTORY_FILE, 4),
        e("market_flow", "시장 수급(토스)", MARKET_FLOW_FILE, 2),
        e("macro", "거시(FRED)", MACRO_FILE, 8, _json_rows(MACRO_FILE)),
        e("macro_kr", "거시(ECOS)", MACRO_KR_FILE, 8, _json_rows(MACRO_KR_FILE)),
        e("company", "기업개황(DART)", COMPANY_PROFILES_FILE, 365, _json_rows(COMPANY_PROFILES_FILE)),
        e("warnings", "투자경고(토스)", WARNINGS_FILE, 2, _json_rows(WARNINGS_FILE)),
        e("gurus", "거장 13F", GURUS_FILE, 40),
        e("us_fund", "미국 재무(EDGAR)", US_FUNDAMENTALS_FILE, 100, _json_rows(US_FUNDAMENTALS_FILE)),
        e("signal_hist", "시그널 히스토리(PIT)", SIGNAL_HISTORY_FILE, 4),
        # 2026-08-05 진단: 이 둘이 freshness 목록에 없어서 **자동 루프에 없다는 사실이 화면에
        # 안 떴다**. us_earnings 가 낡으면 실적 게이트가 조용히 안 걸린다.
        e("fund_hist", "연도별 재무(PIT 백테스트)", FUNDAMENTALS_HISTORY_FILE, 100,
          _json_rows(FUNDAMENTALS_HISTORY_FILE)),
        e("us_earnings", "미국 실적일정(게이트)", US_EARNINGS_FILE, 8),
    ]


def snapshot_signals(signals, date: str | None = None) -> int:
    """오늘의 종목별 시그널·팩터값을 point-in-time으로 기록(일 1회). 수급·퀄리티·정성은 과거 PIT
    데이터가 없어 사전 백테스트가 불가했는데, 오늘부터 쌓아 향후 팩터 백테스트를 가능하게 한다.
    같은 날 재실행 시 그 날짜를 덮어쓴다. 반환: 기록한 종목 수.

    2026-08-03+: rank·gate·reasons·Decision 요약을 같이 남겨 pick-reason 사후 재생이 가능하게 한다.
    구행은 해당 열이 비어 있을 수 있다.
    """
    if not signals:
        return 0
    date = date or datetime.date.today().isoformat()
    # KB 원문 커버리지도 그날 값으로 함께 남긴다 — 나중에 `fetched`로 재구성하면 prune이 지운
    # 문서만큼 과소집계돼(오래된 날짜일수록 심함) '정보 있음/없음' 비교가 편향된다.
    try:
        from signal_desk import db
        kb_docs = db.kb_doc_counts()
    except Exception:
        kb_docs = {}
    from signal_desk.signals import pick_reason as pr
    rows = []
    for s in signals:
        meta = pr.history_meta(s)
        rows.append({
            "date": date, "ticker": s.ticker, "score": round(s.score, 3), "kind": s.kind,
            "technical": round(s.technical_score, 3), "fundamental": round(s.fundamental_score, 3),
            "valuation": s.valuation_percentile, "reversion": round(s.reversion_score, 3),
            "qualitative": s.qualitative_score, "flow": s.flow_intensity,
            "quality": s.quality_points, "momentum": s.momentum_ret,
            "short": s.short_ratio, "kb_docs": kb_docs.get(s.ticker, 0),
            **meta,
        })
    df_new = pd.DataFrame(rows)
    if SIGNAL_HISTORY_FILE.exists():
        old = _read_parquet(SIGNAL_HISTORY_FILE)
        if not old.empty and "date" in old.columns:
            old = old[old["date"] != date]  # 같은 날 재실행 → 갱신
            df_new = pd.concat([old, df_new], ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_parquet(df_new, SIGNAL_HISTORY_FILE)
    return len(rows)


def save_harness_last(result: dict, *, market: str = "kr") -> None:
    """CLI harness 결과를 시그널 판별력 보드가 읽도록 저장. 판정 필드는 harness.run 출력 그대로."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {
        **result,
        "market": market,
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    HARNESS_LAST_FILE.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")


def load_harness_last() -> dict:
    if not HARNESS_LAST_FILE.exists():
        return {"ready": False,
                "reason": "harness 미실행 — 관리자 「시그널 판별력」하네스 실행 또는 `sigdesk harness`"}
    try:
        return json.loads(HARNESS_LAST_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ready": False, "reason": f"harness_last.json 파싱 실패: {type(e).__name__}"}


def signal_config_dict(sc) -> dict:
    """`SignalConfig` → 사전등록·해시와 같은 모양의 dict(`signalcfg.FIELDS` + `selection_mode`).

    `signalcfg.get_dict()`는 라이브 설정만 낸다. 하네스는 임의 설정을 검사하므로 같은 모양을
    만드는 함수가 따로 필요하다 — 모양이 다르면 해시가 달라져 F11·F12 비교가 조용히 어긋난다.
    """
    from signal_desk import signalcfg

    out = {f: getattr(sc, f) for f in signalcfg.FIELDS if hasattr(sc, f)}
    out[signalcfg.MODE_FIELD] = getattr(sc, signalcfg.MODE_FIELD, "rank")
    return out


def run_harness(*, market: str = "kr", top_pct: float = 3.0, hold: int = 5,
                cost: float = 0.25, trials: int = 40, exposure: bool = False,
                signal_config=None, pit: bool = False, pit_fund: bool = False,
                preregistered_id: str | None = None, lock: bool = False,
                threshold_pct: float | None = None, n_registered: int | None = None) -> dict:
    """하네스를 돌리고 **이력에 남긴다**. 보드 정본은 사전등록된 확정 실행만 갱신한다.

    `signal_config`를 안 주면 `signalcfg.get_config()`(소스 기본값 + kv 오버라이드)를 검사한다.
    전에는 인자가 아예 없어 `HarnessConfig`의 `default_factory=SignalConfig`가 걸렸고, 그래서
    **화면·봇이 쓰는 설정과 판정이 재는 설정이 갈라져 있었다** — 가중치를 바꿔도 판정이 안 변했다.
    `effective_config()`가 아니라 `get_config()`인 이유: 국면 적응(익스포저·문턱)은 하네스가 쓰지
    않기로 했고(D10), `effective_config`는 regime·macro·flow를 `api`에서 조립해 받으므로
    `store`가 부르면 순환 import가 된다.

    `preregistered_id`가 없으면 **탐색 실행**이다 — 이력에만 쌓이고 `harness_last.json`을
    건드리지 않는다. 우연히 문턱을 넘은 탐색 결과가 보드에 남는 경로를 코드에서 없앤다.
    """
    from signal_desk import db, prereg, signalcfg
    from signal_desk.signals import harness as hz
    from signal_desk.signals import pit_fundamentals as pf

    market = "us" if market == "us" else "kr"
    if not is_ready():
        return {"ready": False, "reason": "캐시 없음 — 데이터 갱신 후 재시도"}
    uni = load_us_universe() if market == "us" else load_universe()
    if not uni:
        return {"ready": False, "reason": f"{market} 유니버스 없음"}
    panel = hz.build_panel(load_all_dated_closes(), {u["ticker"] for u in uni})
    if len(panel.dates) < 50:
        return {"ready": False, "reason": f"시세 일수 부족({len(panel.dates)})"}

    sc = signal_config if signal_config is not None else signalcfg.get_config()
    cfg = hz.HarnessConfig(
        top_pct=float(top_pct), rebalance_days=int(hold), cost_pct=float(cost),
        random_trials=max(10, min(int(trials), 200)), use_exposure=bool(exposure),
        signal_config=sc,
    )
    scores, source, pit_dates = None, "price", None
    cov6 = fired6 = None
    if pit:
        hdf = load_signal_history()
        if hdf.empty:
            return {"ready": False, "reason": "PIT 스냅샷 없음 — 마감 스냅샷이 쌓여야 한다"}
        scores, meta = hz.scores_from_pit(panel, hdf.to_dict("records"))
        source, pit_dates = "pit", meta.get("pit_dates")
    elif pit_fund:
        hist = load_fundamentals_history()
        if not hist:
            return {"ready": False,
                    "reason": "연도별 재무 없음 — `fetch_fundamentals_history` 를 먼저 돌려야 한다"}
        price_now = {t: [v for v in row if v is not None][-1]
                     for t, row in panel.closes.items() if any(v is not None for v in row)}
        shares = pf.shares_estimate(load_fundamentals(), price_now)
        if not shares:
            return {"ready": False, "reason": "시가총액·현재가가 없어 발행주식수를 근사할 수 없다"}
        scores, cov6, fired6, meta6 = hz.scores_with_pit_fundamentals(
            panel, sc, hist, shares=shares, universe=uni)
        source, pit_dates = "price6", meta6.get("fund_dates")
    regimes = (hz.regimes_at(panel, hz._rebalance_indices(panel, cfg))
               if cfg.use_exposure else None)
    out = (hz.run(panel, cfg, regimes, scores=scores, score_source=source,
                  coverage=cov6, fired=fired6) if scores is not None
           else hz.run(panel, cfg, regimes))
    if not out.get("ready"):
        return out

    cfg_dict = signal_config_dict(sc)
    hz_dict = {"hold": cfg.rebalance_days, "cost_pct": cfg.cost_pct,
               "trials": cfg.random_trials, "exposure": cfg.use_exposure,
               "top_pct": cfg.top_pct, "seed": cfg.seed}
    db.harness_run_insert({
        "preregistered_id": preregistered_id, "score_source": out.get("score_source") or source,
        "market": market,
        "config_json": json.dumps(cfg_dict, ensure_ascii=False, sort_keys=True),
        "config_hash": prereg.config_hash(cfg_dict),
        "harness_json": json.dumps(hz_dict, ensure_ascii=False, sort_keys=True),
        "percentile": (out.get("vs_random") or {}).get("percentile"),
        "threshold_pct": threshold_pct, "n_registered": n_registered,
        "periods": out.get("periods"), "empty_periods": out.get("empty_periods"),
        "effective_periods": out.get("effective_periods"),
        "pit_dates": pit_dates, "price_data_to": panel.dates[-1] if panel.dates else None,
        "verdict": out.get("verdict"), "verdict_why": out.get("verdict_why"),
        "is_locked": bool(lock and preregistered_id),
        "warnings_json": json.dumps(out.get("warnings") or [], ensure_ascii=False),
        "note": None if preregistered_id else "탐색 실행 — 보드 정본 아님",
    })

    blob = {**out, "top_pct": cfg.top_pct, "hold_days": cfg.rebalance_days,
            "trials": cfg.random_trials, "signal_config": cfg_dict,
            "config_hash": prereg.config_hash(cfg_dict),
            "preregistered_id": preregistered_id}
    if lock and preregistered_id:
        save_harness_last(blob, market=market)
        return load_harness_last()
    return {**blob, "board_updated": False}


def load_signal_history():
    if not SIGNAL_HISTORY_FILE.exists():
        return pd.DataFrame()
    return _read_parquet(SIGNAL_HISTORY_FILE)


# 연속 두 날짜 사이에 점수가 바뀐 종목 비율이 이 값 이하면 '동결'로 본다. 시세가 갱신되면
# 기술·모멘텀 팩터가 소수점 셋째 자리까지 같을 수 없으므로, 5%는 넉넉한 하한이다.
_DRIFT_FROZEN_PCT = 5.0


def signal_drift(pairs: int = 3) -> dict:
    """PIT 스냅샷에서 종목 점수가 실제로 날마다 움직였는지 검사.

    시세 갱신이 멈추면 점수가 얼어붙고, 그러면 문턱·분위를 어떻게 조정해도 매일 같은 결과가
    나온다 — "시그널이 안 나온다"의 1순위 용의자라서 진단 화면에 상시 노출한다.
    반환: {available, frozen, pairs:[{from,to,changed_pct,universe}], note}
    """
    df = load_signal_history()
    if df.empty or not {"date", "ticker", "score"} <= set(df.columns):
        return {"available": False, "frozen": None, "pairs": [],
                "note": "PIT 스냅샷 없음 — 장 마감 후 스냅샷이 쌓이면 판정 가능"}
    dates = sorted(df["date"].astype(str).unique())[-(pairs + 1):]
    if len(dates) < 2:
        return {"available": False, "frozen": None, "pairs": [],
                "note": f"스냅샷 {len(dates)}일치 — 2일 이상 필요"}
    piv = (df[df["date"].astype(str).isin(dates)]
           .assign(date=lambda d: d["date"].astype(str))
           .pivot_table(index="ticker", columns="date", values="score"))
    out = []
    for a, b in zip(dates, dates[1:]):
        if a not in piv.columns or b not in piv.columns:
            continue
        both = piv[[a, b]].dropna()
        n = len(both)
        changed = int((both[a].round(3) != both[b].round(3)).sum())
        out.append({"from": a, "to": b, "universe": n,
                    "changed_pct": round(changed / n * 100, 1) if n else None})
    latest = out[-1]["changed_pct"] if out else None
    frozen = latest is not None and latest <= _DRIFT_FROZEN_PCT
    return {"available": bool(out), "frozen": frozen, "pairs": out,
            "note": ("점수가 사실상 그대로다 — 시세 갱신 중단 의심(토스/KIS 인증·수집 실패 확인)"
                     if frozen else "점수가 날마다 갱신되고 있음")}


def load_all_dated_closes() -> dict[str, tuple[list[str], list[float]]]:
    """ticker -> (dates[], closes[]) 오래된→최신, 국내+미국 통합. 실측 성과(accuracy) 조인용.
    각 parquet을 1회만 읽어 종목별 (날짜, 종가) 짝을 만든다(실시간 잠정봉은 제외 — 성숙 판정 왜곡 방지)."""
    out: dict[str, tuple[list[str], list[float]]] = {}
    for f in (PRICES_FILE, US_PRICES_FILE):
        if not f.exists():
            continue
        df = _read_parquet(f)
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
    df = _read_parquet(PRICES_FILE)
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


# ------------------------------------------------------- 사전등록 판정 보드 (PRD N3)

def pit_dates_count() -> int:
    """PIT 스냅샷이 있는 **거래일 수**. 요건 진척의 분자다(행 수가 아니라 날짜 수).

    행으로 세면 하루 200종목이 200관측으로 부풀어 요건이 즉시 충족된 것처럼 보인다 —
    같은 날 200종목은 하나의 관측이다.
    """
    df = load_signal_history()
    if df.empty or "date" not in df.columns:
        return 0
    return int(df["date"].astype(str).nunique())


def _signal_config_from(cfg: dict):
    from dataclasses import replace

    from signal_desk.signals.engine import SignalConfig
    base = SignalConfig()
    known = {k: v for k, v in (cfg or {}).items() if hasattr(base, k)}
    return replace(base, **known)


def run_preregistered(look_id: str, *, path=None) -> dict:
    """사전등록된 look 하나를 그대로 실행한다. **정본이 될 수 있는 유일한 경로.**

    잠금(확정) 판단을 호출자에게 맡기지 않는다 — 요건 충족 여부를 이 함수가 실행 결과로
    직접 계산하고, 이미 확정된 look이면 다시 잠그지 않는다(요건 충족일 1회 판정 후 동면).
    """
    from signal_desk import db, prereg

    reg = prereg.load(path)
    if not reg["ok"]:
        return {"ready": False, "reason": reg["reason"]}
    look = next((lk for lk in reg["looks"] if lk["id"] == look_id), None)
    if look is None:
        return {"ready": False, "reason": f"사전등록에 없는 id: {look_id}"}

    ok, why = prereg.config_agrees_with_engine(look["config"])
    if not ok:
        return {"ready": False, "reason": why}

    hzc = look["harness"]
    pit = look["score_source"] == "pit"
    pit_fund = look["score_source"] == "price6"
    pre = pit_dates_count() if pit else None

    # 잠금 여부는 실행 결과의 실효 기간으로 정해진다 → 먼저 돌리고, 요건을 만족하면 그 실행을 잠근다.
    # 1패스로 끝내려고 run_harness에 lock을 두 번 넘기지 않고, 여기서 미리 계산할 수 있는 것만 계산한다.
    already = db.harness_locked_run(look_id)
    out = run_harness(
        market=look["market"], top_pct=float(hzc.get("top_pct") or look["config"].get("rank_top_pct") or 3.0),
        hold=int(hzc.get("hold") or 5), cost=float(hzc.get("cost_pct") or 0.25),
        trials=int(hzc.get("trials") or 200), exposure=bool(hzc.get("exposure") or False),
        signal_config=_signal_config_from(look["config"]), pit=pit, pit_fund=pit_fund,
        preregistered_id=look_id, lock=False,
        threshold_pct=reg["threshold_pct"], n_registered=reg["n_canonical"])
    if not out.get("ready"):
        return out
    prog = prereg.progress(look, effective_periods=out.get("effective_periods") or 0,
                           pit_dates=(pre if pit else out.get("pit_dates")) or 0)
    if prog["met"] and not already:
        # 요건 충족 첫 실행 — 같은 실행을 잠긴 것으로 다시 남기고 보드를 갱신한다.
        return run_harness(
            market=look["market"], top_pct=float(hzc.get("top_pct") or look["config"].get("rank_top_pct") or 3.0),
            hold=int(hzc.get("hold") or 5), cost=float(hzc.get("cost_pct") or 0.25),
            trials=int(hzc.get("trials") or 200), exposure=bool(hzc.get("exposure") or False),
            signal_config=_signal_config_from(look["config"]), pit=pit, pit_fund=pit_fund,
            preregistered_id=look_id, lock=True,
            threshold_pct=reg["threshold_pct"], n_registered=reg["n_canonical"])
    return {**out, "progress": prog, "board_updated": False}


def harness_board(market: str = "kr", *, path=None) -> dict:
    """`/api/proof` A.harness — 사전등록 기준 판정 보드.

    **요건 미충족 상태에서 백분위를 내지 않는다.** 매일 보이면 매일 보게 되고 그게 peeking이다.
    이력 표에서는 볼 수 있다 — 이력은 진단용이고 보드는 판정용이다.
    """
    from signal_desk import db, prereg

    reg = prereg.load(path)
    if not reg["ok"]:
        return {"ready": False, "status": "unregistered", "reason": reg["reason"],
                "verdict": "판정 불가", "verdict_why": reg["reason"]}

    pit_dates = pit_dates_count()
    looks_out = []
    for lk in reg["looks"]:
        if lk["market"] != market:
            continue
        locked = db.harness_locked_run(lk["id"])
        cur_hash = prereg.config_hash(lk["config"])
        status = prereg.board_status(locked, current_hash=cur_hash)
        # 실효 기간은 하네스를 돌려야 정확하다. 매일 돌리지 않으므로 (a) 마지막 실행의 실측값,
        # (b) 없으면 pit_dates//hold 상한 추정을 쓰고 **어느 쪽인지 라벨을 붙인다**.
        recent = next((r for r in db.harness_runs_recent(200)
                       if r["preregistered_id"] == lk["id"]), None)
        hold = int((lk["harness"] or {}).get("hold") or 5)
        if recent and recent.get("effective_periods") is not None:
            eff, eff_src = int(recent["effective_periods"]), "measured"
        else:
            eff, eff_src = pit_dates // max(1, hold), "estimated"
        prog = prereg.progress(lk, effective_periods=eff, pit_dates=pit_dates)
        prog["effective_periods_source"] = eff_src
        row = {
            "id": lk["id"], "role": lk["role"], "family": lk["family"],
            "hypothesis": lk["hypothesis"], "score_source": lk["score_source"],
            "status": status, "threshold_pct": reg["threshold_pct"],
            "n_registered": reg["n_canonical"], "config_hash": cur_hash,
            "requirement": prog, "decision": lk["decision"],
            "last_run_at": recent["ran_at"] if recent else None,
        }
        if status == "locked":
            row.update(percentile=locked.get("percentile"), verdict=locked.get("verdict"),
                       verdict_why=locked.get("verdict_why"),
                       locked={"at": locked.get("ran_at"), "config_hash": locked.get("config_hash")})
        elif status == "invalidated":
            row.update(percentile=None, verdict="무효",
                       verdict_why="판정 이후 설정이 바뀌었다 — 새 id로 재등록해야 한다")
        else:
            miss = []
            if prog["remaining_effective_periods"]:
                miss.append(f"실효 기간 {prog['effective_periods']}/{prog['min_effective_periods']}")
            if prog["remaining_pit_dates"]:
                miss.append(f"PIT {prog['pit_dates']}/{prog['min_pit_dates']}일")
            row.update(percentile=None, verdict="판정 보류",
                       verdict_why=" · ".join(miss) or "요건 충족 — 다음 실행에서 확정")
        looks_out.append(row)

    final = next((r for r in looks_out if r["role"] == "final"), None)
    head = final or (looks_out[0] if looks_out else None)
    return {
        "ready": True, "market": market,
        "threshold_pct": reg["threshold_pct"], "n_registered": reg["n_canonical"],
        "status": head["status"] if head else "unregistered",
        "verdict": head["verdict"] if head else "판정 불가",
        "verdict_why": head["verdict_why"] if head else "이 시장에 등록된 look이 없다",
        "percentile": head.get("percentile") if head else None,
        "requirement": head["requirement"] if head else None,
        "looks": looks_out,
        "note": ("정본은 role=final이다. interim은 중간 판독이며 채택 근거가 아니다. "
                 "요건 미충족 동안 백분위는 보드에 내지 않는다(매일 보는 것이 곧 다중검정)."),
    }


# ------------------------------------------------------- 정지 탐지 (N1)

def pit_gap_days(limit_dates: int = 60) -> dict:
    """PIT 스냅샷이 **거래일인데 비어 있는** 날을 센다.

    왜 mtime으로 안 되나: 시세가 백필로 최신이면 `stale_prices=false`가 되어 **스냅샷 정지를
    가린다**. 실제로 2026-08 진단에서 시세는 08-04까지 정상인데 스냅샷은 며칠 비어 있었고
    `blocked_reason`은 null이었다 — 어느 플래그도 안 잡았다. 그래서 파일 신선도가 아니라
    **거래일 달력과 대조**한다.
    """
    df = load_signal_history()
    if df.empty or "date" not in df.columns:
        return {"pit_dates": 0, "missing": [], "missing_n": 0, "from": None, "to": None,
                "reason": "PIT 스냅샷 없음"}
    snap = sorted({str(d) for d in df["date"].astype(str)})
    market = [d for d in _market_dates() if snap[0] <= d <= snap[-1]]
    if limit_dates and len(market) > limit_dates:
        market = market[-limit_dates:]
    have = set(snap)
    missing = [d for d in market if d not in have]
    return {"pit_dates": len(snap), "missing": missing, "missing_n": len(missing),
            "from": snap[0], "to": snap[-1], "reason": None}


def stall_report() -> dict:
    """수집이 멈췄는지 한 번에 판단할 재료. 브리핑 첫 줄·관리자 배너가 같은 함수를 쓴다.

    같은 판단을 두 곳에서 조립하면 화면과 알림이 다른 말을 하게 된다.
    """
    import datetime as _dt

    fresh = data_freshness()
    stale = [{"key": e["key"], "label": e["label"], "updated": e["updated"],
              "age_hours": e["age_hours"]}
             for e in fresh if e.get("stale")]
    missing_file = [e for e in stale if not e.get("updated")]
    gap = pit_gap_days()

    harness_days = None
    last_run = None
    try:
        from signal_desk import db
        runs = db.harness_runs_recent(1)
        if runs:
            last_run = runs[0]["ran_at"]
            ran = _dt.datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
            harness_days = (_dt.datetime.now(_dt.timezone.utc) - ran).days
    except Exception:                                 # noqa: BLE001 — 없으면 None(이유는 화면에)
        harness_days = None

    return {
        "ok": not stale and not gap["missing_n"],
        "stale": stale,
        "missing_files": [e["label"] for e in missing_file],
        "pit": gap,
        "harness_days": harness_days,
        "harness_last_run": last_run,
    }


def decision_baseline(entry_dates: list[str], horizon_days: int) -> dict:
    """봇 판단 승률에 붙일 **같은 관례의 기준선** — "그 날 아무거나 샀으면".

    같은 진입일·같은 지평·같은 진입/청산 관례(종가→종가)로 유니버스 전체를 센다. 관례가 하나라도
    다르면 리프트가 거짓이 된다 — 2026-08-05 진단에서 `win_rate`(장중 진입·가변 지평)와
    `baseline_buy_pct`(익일 종가·정확히 5거래일)를 비교해 "+0.4%p"라는 없는 숫자를 만들었다.

    반환: `{up_pct, sample, avg_ret_pct, dates}`. 표본이 없으면 `up_pct=None`.
    """
    if not entry_dates or horizon_days <= 0:
        return {"up_pct": None, "sample": 0, "avg_ret_pct": None, "dates": 0}
    dated = load_all_dated_closes()
    wins = tot = 0
    rets: list[float] = []
    used: set[str] = set()
    for ticker, pair in dated.items():
        dates, closes = pair
        idx = {d: i for i, d in enumerate(dates)}
        for ed in entry_dates:
            i = idx.get(ed)
            if i is None:
                continue
            j = i + horizon_days
            if j >= len(closes):
                continue
            a, b = closes[i], closes[j]
            if not a or not b:
                continue
            r = (b / a - 1) * 100
            rets.append(r)
            tot += 1
            wins += 1 if r > 0 else 0
            used.add(ed)
    if not tot:
        return {"up_pct": None, "sample": 0, "avg_ret_pct": None, "dates": 0}
    return {"up_pct": round(wins / tot * 100, 1), "sample": tot,
            "avg_ret_pct": round(sum(rets) / tot, 2), "dates": len(used)}


def decision_scorecard_with_baseline() -> dict:
    """스코어카드 + 같은 관례 기준선 + 리프트 + 신뢰구간 반폭.

    비율만 내보내면 읽는 사람이 좋은지 나쁜지 모른다. **base rate 없는 비율은 화면에 내지 않는다** —
    그래서 이 함수가 붙기 전의 `win_rate` 단독 노출은 규칙 위반이었다.
    """
    from signal_desk import db

    card = db.bot_decision_scorecard()
    hz = card.get("horizon_days") or []
    h = hz[0] if len(hz) == 1 else None
    base = (decision_baseline(card.get("entry_dates") or [], h) if h
            else {"up_pct": None, "sample": 0, "avg_ret_pct": None, "dates": 0})
    wr, n = card.get("win_rate"), card.get("resolved") or 0
    lift = round(wr - base["up_pct"], 1) if (wr is not None and base["up_pct"] is not None) else None
    # 이항 표준오차 × 1.96. n이 작으면 리프트가 오차 안에 들어가고, 그러면 무정보라고 말해야 한다.
    ci = None
    if wr is not None and n:
        p = wr / 100
        ci = round(1.96 * ((p * (1 - p) / n) ** 0.5) * 100, 1)
    return {**card, "baseline": base, "lift_pp": lift, "ci_pp": ci,
            "horizon_note": (f"판단일 다음 거래일 종가 진입 → {h}거래일 뒤 종가 청산(기준선과 동일 관례)"
                             if h else "지평이 섞여 있어 기준선을 붙일 수 없다"),
            "informative": bool(lift is not None and ci is not None and abs(lift) > ci)}
