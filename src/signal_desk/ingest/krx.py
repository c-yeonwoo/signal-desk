"""KRX 시세 수집 — pykrx(시계열) + KRX Open API(유니버스) 조합.

주의(2026-07 직접 확인): pykrx의 "시장 전체/지수 구성종목" 계열 엔드포인트
(`get_index_portfolio_deposit_file`, `get_market_cap_by_ticker`, `get_market_ohlcv_by_ticker`,
`get_market_ticker_list`)가 전부 `Expecting value: line 1 column 1` JSON 파싱 에러로 깨져 있다 —
KRX가 해당 응답 스키마를 바꿨고 pykrx 최신(1.2.8)이 아직 못 따라간 것으로 보인다(재현 확인됨).
반면 **종목별 시계열**(`get_market_ohlcv_by_date`)과 **단일 종목명 조회**(`get_market_ticker_name`)는
정상 동작한다 — 이건 계속 pykrx를 쓴다.

유니버스는 `KRX_API_KEY`가 있고 서비스 승인이 됐으면 시가총액 상위 종목(코스피200 근사,
`krx_open_api.universe_by_marketcap` — 필드명 미검증 상태, ⚠️ 주석 참고)을 쓰고, 실패하면
(키 없음/서비스 미승인/응답 구조 불일치) 잘 알려진 대형주 임시 리스트로 폴백한다. 상위
레이어(engine/store/api)는 `universe()`의 반환 형태에만 의존하므로 내부 소스 교체는 영향 없음.
"""

from __future__ import annotations

import datetime
import logging

from pykrx import stock

from signal_desk import config
from signal_desk.ingest import krx_open_api

log = logging.getLogger("signal_desk.ingest.krx")

# 코스피200 편입종목을 프로그래밍적으로 못 가져올 때(키 미설정/서비스 미승인)의 최종 폴백.
_INTERIM_TICKERS = [
    "005930", "000660", "373220", "207940", "005380", "000270", "005490", "035420",
    "068270", "035720", "051910", "006400", "105560", "055550", "012330", "032830",
    "003670", "066570", "028260", "015760", "034730", "018260", "010130", "042660",
    "011200", "086790", "024110", "138040", "323410", "259960",
]


def _interim_universe() -> list[dict]:
    out = []
    for code in _INTERIM_TICKERS:
        try:
            name = stock.get_market_ticker_name(code)
        except Exception:
            name = None
        if not name or not isinstance(name, str):
            log.warning("유니버스 종목코드 검증 실패, 제외: %s", code)
            continue
        out.append({"ticker": code, "name": name})
    return out


def universe(limit: int = 200) -> list[dict]:
    """유니버스(ticker+name) 목록. KRX_API_KEY 서비스 승인 시 시가총액 상위 `limit`종목(코스피200
    근사), 아니면 대형주 30종목 폴백."""
    if config.krx_key():
        today = datetime.date.today()
        for delta in range(5):  # 최근 영업일 탐색(주말/공휴일 대비)
            bas_dd = (today - datetime.timedelta(days=delta)).strftime("%Y%m%d")
            items = krx_open_api.universe_by_marketcap(bas_dd, limit=limit)
            if items:
                return items
        log.warning("KRX Open API 유니버스 조회 실패(서비스 미승인 가능성) — 임시 리스트로 폴백")
    return _interim_universe()


def ohlcv(ticker: str, start: str, end: str) -> list[dict]:
    """종목별 일봉(시가/종가/거래량). start/end는 'YYYYMMDD'. 오래된→최신 순 정렬."""
    df = stock.get_market_ohlcv_by_date(start, end, ticker)
    if df is None or df.empty:
        return []
    df = df.sort_index()
    return [
        {"date": idx.strftime("%Y-%m-%d"), "open": float(row["시가"]), "close": float(row["종가"]),
         "volume": float(row.get("거래량", 0) or 0)}
        for idx, row in df.iterrows()
    ]


def investor_flows(ticker: str, start: str, end: str) -> dict | None:
    """기간(start~end, 'YYYYMMDD') 투자자별 순매수대금 — 외국인·기관 순매수와 전체 거래대금.
    한국 시장 핵심 수급 팩터. pykrx 응답 스키마 변동 대비 방어적(실패 시 None). 반환:
    {foreign_net, inst_net, total_buy}(원)."""
    try:
        df = stock.get_market_trading_value_by_investor(start, end, ticker)
    except Exception as e:
        # pykrx 내부에서 나는 예외(주로 KRX 응답이 비었거나 스키마가 바뀐 경우) — 어떤 키/값이
        # 문제인지 알아야 원인(응답 empty / 스키마 변경 / IP 차단)을 짚으므로 예외 전문을 남긴다.
        log.warning("수급(투자자별 거래대금) 수집 실패(%s): %r", ticker, e)
        return None
    if df is None or df.empty or "순매수" not in df.columns:
        return None

    def _net(label: str) -> float:
        try:
            return float(df.loc[label, "순매수"]) if label in df.index else 0.0
        except (TypeError, ValueError, KeyError):
            return 0.0

    foreign = _net("외국인") + _net("기타외국인")
    inst = _net("기관합계")
    try:
        total_buy = float(df.loc["전체", "매수"]) if "전체" in df.index and "매수" in df.columns else 0.0
    except (TypeError, ValueError, KeyError):
        total_buy = 0.0
    return {"foreign_net": foreign, "inst_net": inst, "total_buy": total_buy}
