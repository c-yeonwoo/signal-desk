"""거래소 세션 시계. 예정된 정규장만 나타내며 시세 신선도나 브로커 주문 가능성을 보증하지 않는다.

모든 비교는 timezone-aware UTC로 수행한다. 캘린더 범위 밖 또는 일정 조회 실패는 닫힘으로
처리한다. 실제 주문에는 이 예정 시계 외에 브로커의 당일 운영시간·시세 시각 검증이 필요하다.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

_MARKETS = {"kr": ("XKRX", "Asia/Seoul"), "us": ("XNYS", "America/New_York")}
_KR_AUCTION_BUFFER = dt.timedelta(minutes=10)


@lru_cache(maxsize=2)
def _calendar(market: str):
    return xcals.get_calendar(_MARKETS[market][0])


def is_session(market: str, day: dt.date | str) -> bool:
    """거래소가 예정한 거래일인지 확인. 모르는 날을 평일로 추정하지 않는다."""
    try:
        date = dt.date.fromisoformat(day) if isinstance(day, str) else day
        cal = _calendar(market)
        if not cal.first_session.date() <= date <= cal.last_session.date():
            return False
        return bool(cal.is_session(date.isoformat()))
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def regular_window(market: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """현재 현지 날짜의 정규장 UTC 구간. 국내는 동시호가 전 10분에 매매봇을 멈춘다."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    day = now.astimezone(ZoneInfo(_MARKETS[market][1])).date()
    if not is_session(market, day):
        return None
    row = _calendar(market).schedule.loc[day.isoformat()]
    start = row["open"].to_pydatetime()
    end = row["close"].to_pydatetime()
    if market == "kr":
        end -= _KR_AUCTION_BUFFER
    return start, end


def is_open(market: str, now: dt.datetime | None = None) -> bool:
    """예정된 연속매매 시간. DST·휴장·조기마감은 거래소 캘린더에서 판정한다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    window = regular_window(market, now)
    return bool(window and window[0] <= now < window[1])


def latest_completed_session(market: str, now: dt.datetime) -> str | None:
    """현재 시각 전에 공식 마감한 최근 세션 날짜. 캘린더 범위 밖이면 알 수 없음."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    cal = _calendar(market)
    if not cal.first_session.date() <= now.astimezone(ZoneInfo(_MARKETS[market][1])).date() <= cal.last_session.date():
        return None
    completed = cal.schedule[cal.schedule["close"] < now]
    return completed.index[-1].date().isoformat() if not completed.empty else None


def consecutive_sessions(market: str, first: str, second: str) -> bool:
    """두 날짜가 연속 거래 세션인지. 누락된 평가일을 성과 0일로 이어 붙이지 않는다."""
    if not is_session(market, first) or not is_session(market, second) or first >= second:
        return False
    sessions = _calendar(market).sessions_in_range(first, second)
    return len(sessions) == 2


def previous_session(market: str, day: str) -> str | None:
    """해당 세션 이전의 마지막 거래일(당일 종가 이후 공개된 구성종목은 매수 시작에 쓰지 않음)."""
    if not is_session(market, day):
        return None
    try:
        return _calendar(market).previous_session(day).date().isoformat()
    except (KeyError, ValueError, IndexError):
        return None
