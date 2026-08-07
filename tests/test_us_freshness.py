"""미국 시세 신선도는 거래일로 센다 + 퀄리티는 날짜가 아니라 있는지로 센다.

두 버그 다 2026-08-07 프로덕션 진단에서 나왔고, 둘 다 **화면이 `ok` 라고 말하는 동안** 죽어
있었다. 공통 원인은 "날짜를 봤지만 그 날짜가 답하는 질문이 아니었다"다.
"""

from __future__ import annotations

import datetime

from signal_desk import store


def _kst(y, m, d, h):
    return datetime.datetime(y, m, d, h, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))


# ─────────────────────────── 기대 마지막 봉 ───────────────────────────

def test_expected_last_bar_walks_back_over_the_weekend():
    """월요일 아침에 기대하는 마지막 봉은 **금요일**이다 — 달력일로 3일이지만 정상이다."""
    assert _kst(2026, 8, 10, 9).weekday() == 0                  # 월요일임을 못박는다
    assert store.us_expected_last_bar(_kst(2026, 8, 10, 9)) == "2026-08-07"
    # 토요일에도 금요일 봉이 최신이다.
    assert store.us_expected_last_bar(_kst(2026, 8, 8, 9)) == "2026-08-07"


def test_expected_last_bar_waits_for_the_us_close_to_land():
    """미국장 종가는 KST 익일 새벽에 확정된다 — 그 전에는 하루 더 뒤를 기대해야 한다.

    안 그러면 매일 아침 "하루 밀림"이 떠서 배너가 곧 안 읽힌다.
    """
    assert store.us_expected_last_bar(_kst(2026, 8, 7, 9)) == "2026-08-06"   # 마감 반영 후
    assert store.us_expected_last_bar(_kst(2026, 8, 7, 3)) == "2026-08-05"   # 아직 전


# ─────────────────────────── 빠진 거래일 ───────────────────────────

def test_missing_trading_days_are_named_and_skip_weekends():
    """"2건 밀림"만 적으면 어느 날이 빈지 몰라 조사가 안 된다 — 이름으로 낸다."""
    assert store.us_missing_trading_days("2026-08-04", "2026-08-06") == \
        ["2026-08-05", "2026-08-06"]
    # 금요일 봉 → 월요일 기대: 주말은 거래일이 아니므로 결손이 없다.
    assert store.us_missing_trading_days("2026-08-07", "2026-08-07") == []
    assert store.us_missing_trading_days("2026-08-07", "2026-08-10") == ["2026-08-10"]


# ─────────────────────────── 회귀: 달력일 문턱이 놓친 것 ───────────────────────────

def test_three_calendar_days_on_weekdays_is_stale(monkeypatch):
    """**이 케이스가 프로덕션에서 통과했다.**

    마지막 봉 08-04, 오늘 08-07. 옛 문턱은 `cutoff = today - 3 = 08-04` 와 비교해
    `"08-04" < "08-04"` 가 거짓 → 갱신 대상 0건 · 화면 `stale=false · ok` 였다. 그 사이
    08-05(수)·08-06(목) 미국장이 둘 다 닫혀 **거래일 2일이 비어 있었다.**
    """
    monkeypatch.setattr(store, "us_price_last_dates", lambda: {"AAPL": "2026-08-04"})
    monkeypatch.setattr(store, "us_expected_last_bar", lambda as_of=None: "2026-08-06")
    assert store.us_prices_stale_tickers(["AAPL"]) == ["AAPL"]


def test_friday_bar_on_monday_is_not_stale(monkeypatch):
    """달력일 문턱이 애초에 3일이었던 이유(주말 오탐)는 그대로 막아야 한다.

    이걸 안 지키면 매주 월요일 503종목을 헛되게 재수집한다 — 완화가 아니라 오탐 방지다.
    """
    monkeypatch.setattr(store, "us_price_last_dates", lambda: {"AAPL": "2026-08-07"})
    monkeypatch.setattr(store, "us_expected_last_bar", lambda as_of=None: "2026-08-07")
    assert store.us_prices_stale_tickers(["AAPL"]) == []


def test_one_missing_trading_day_is_tolerated_as_a_possible_holiday(monkeypatch):
    """공휴일 달력이 없으므로 1거래일 갭은 통과시킨다 — **그래서 못 잡는다는 사실을 못박는다.**

    여유를 0으로 두면 미국 공휴일마다 전 종목이 갱신 대상이 되어 무한 재수집이 된다.
    """
    monkeypatch.setattr(store, "us_price_last_dates", lambda: {"AAPL": "2026-08-05"})
    monkeypatch.setattr(store, "us_expected_last_bar", lambda as_of=None: "2026-08-06")
    assert store.us_prices_stale_tickers(["AAPL"]) == []


def test_ticker_with_no_bars_at_all_is_a_refresh_target(monkeypatch):
    monkeypatch.setattr(store, "us_price_last_dates", lambda: {})
    monkeypatch.setattr(store, "us_expected_last_bar", lambda as_of=None: "2026-08-06")
    assert store.us_prices_stale_tickers(["AAPL"]) == ["AAPL"]


# ─────────────────────────── 퀄리티: 날짜가 아니라 있는지 ───────────────────────────

def test_quality_freshness_separates_no_financials_from_uncomputed(monkeypatch):
    """0의 이유를 가른다 — 재무가 없는 것(상위 고장)과 파생값 미계산(이번 병)은 다른 문장이다."""
    monkeypatch.setattr(store, "load_fundamentals", lambda: {})
    e = store._quality_freshness()
    assert e["stale"] and "재무(DART)가 없어" in e["note"]

    # 재무는 있는데 퀄리티가 안 붙은 상태 — 프로덕션 실측 0/200이 이것이었다.
    monkeypatch.setattr(store, "load_fundamentals",
                        lambda: {"005930": {"net_income": 1, "roe": 10.0}})
    e = store._quality_freshness()
    assert e["stale"] and "퀄리티가 0건" in e["note"]
    assert e["rows"] == 0 and e["total"] == 1


def test_quality_freshness_is_not_reported_as_a_missing_file(monkeypatch):
    """파생값은 자기 파일이 없다 — 안 걸러 내면 배너가 "파일 없음"이라 잘못 보고한다."""
    monkeypatch.setattr(store, "load_fundamentals",
                        lambda: {"005930": {"net_income": 1, "roe": 10.0}})
    e = store._quality_freshness()
    assert e["kind"] == "derived" and e["updated"] is None
    report = store.stall_report()
    assert "회사 체질(재무 파생)" not in report["missing_files"]


def test_quality_attached_count_counts_has_not_presence_of_the_key(monkeypatch):
    """`quality` 키가 있어도 `has=False` 면 가중치가 0이다 — 키 유무로 세면 고장을 놓친다."""
    monkeypatch.setattr(store, "load_fundamentals", lambda: {
        "A": {"quality": {"has": True, "points": 3}},
        "B": {"quality": {"has": False, "points": 0}},     # 계산했지만 근거 부족
        "C": {},                                            # 아예 없음
    })
    assert store.quality_attached_count() == 1
