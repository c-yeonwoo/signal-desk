"""신선도를 달력일로 재서 **매주 월요일 오탐**이 났다(2026-08-17 프로덕션).

실측 배너:

    🔧 갱신 멈춤 국내 시세(2일) · 미국 시세 1/504종목 뒤처짐 · 종목 수급(네이버)(2일) 외 3개

그런데 그날은 월요일 오전이고 마지막 거래일은 금요일(08-14)이었다 — **데이터는 최신**이었다.
장이 안 서는 날을 세면 금요일 마감 뒤 월요일 아침마다 국내 소스 전부가 stale이 된다.
매주 우는 늑대는 곧 안 읽힌다(이 리포는 "정상일 때는 아무 말도 하지 않는다"를 규칙으로 뒀다).

이 리포는 PIT 스냅샷에 대해 **이미 같은 것을 배웠다** — "정지는 파일 신선도로 안 잡힌다,
거래일 달력과 대조한다"(`pit_gap_days`). 여기가 세 번째 재발이다.

기존 우회는 문턱을 4일로 늘리는 것이었는데(`consensus`·`signal_hist`), 그건 **평일 3일 정지를
못 잡는** 대가를 치른다. 완화가 아니라 정확해져야 한다.

왜 `_market_dates()`(시세 파일)를 안 쓰나: 그 파일이 멈추면 달력도 같이 멈춰 경과가 영원히
0이 된다 — fail-open이라 게이트가 없는 것과 같다. 그래서 파일과 무관한 **평일 수**로 센다.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from signal_desk import store

KST = ZoneInfo("Asia/Seoul")


def _ts(y, m, d, hh=6, mm=43):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=KST).timestamp()


@pytest.fixture()
def monday_morning(monkeypatch):
    """2026-08-17(월) 09:00 KST — 마지막 거래일은 08-14(금). 실측 상황 그대로."""
    real = datetime.datetime

    class _Now(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 8, 17, 9, 0, tzinfo=tz or KST)

    monkeypatch.setattr(store.datetime, "datetime", _Now)
    return None


def test_friday_data_is_not_stale_on_monday_morning(monday_morning):
    """**이게 그 오탐이다.** 금요일 마감 데이터는 월요일 아침에 최신이다."""
    assert store._weekday_age_days(_ts(2026, 8, 14)) == 0.0


def test_weekend_does_not_count(monday_morning):
    """토·일은 장이 안 선다 — 세면 매주 오탐이다."""
    assert store._weekday_age_days(_ts(2026, 8, 14)) < store._weekday_age_days(_ts(2026, 8, 13))
    # 달력으로는 금→월이 3일이지만 평일로는 0일
    cal = (datetime.date(2026, 8, 17) - datetime.date(2026, 8, 14)).days
    assert cal == 3 and store._weekday_age_days(_ts(2026, 8, 14)) == 0.0


def test_a_real_outage_is_still_caught(monday_morning):
    """**완화가 아니다.** 진짜 정지는 여전히 잡혀야 한다 — 안 그러면 게이트가 없는 것과 같다."""
    assert store._weekday_age_days(_ts(2026, 8, 5)) == 7.0     # 8/5(수) → 8/14(금)
    assert store._weekday_age_days(_ts(2026, 8, 13)) == 1.0    # 목 → 금


def test_today_is_not_expected_before_the_close(monkeypatch):
    """장 마감 전에는 오늘 데이터를 기대하지 않는다 — 기대하면 매일 아침이 stale이다."""
    real = datetime.datetime

    def _at(hour):
        class _Now(real):
            @classmethod
            def now(cls, tz=None):
                return real(2026, 8, 18, hour, 0, tzinfo=tz or KST)   # 화요일
        return _Now

    monkeypatch.setattr(store.datetime, "datetime", _at(9))
    assert store._weekday_age_days(_ts(2026, 8, 17)) == 0.0, "장중인데 어제 데이터를 낡았다고 한다"
    monkeypatch.setattr(store.datetime, "datetime", _at(18))
    assert store._weekday_age_days(_ts(2026, 8, 17)) == 1.0, "마감 뒤엔 오늘 데이터를 기대한다"


def test_market_sources_count_trading_days_and_others_do_not(tmp_path, monkeypatch):
    """시장 데이터만 평일로 센다 — FRED·13F는 자기 일정으로 나오므로 달력일이 맞다."""
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    for name in ("PRICES_FILE", "FLOWS_FILE", "SHORT_FILE", "WARNINGS_FILE", "MARKET_FLOW_FILE",
                 "CONSENSUS_HISTORY_FILE", "SIGNAL_HISTORY_FILE", "MACRO_FILE", "GURUS_FILE"):
        p = tmp_path / getattr(store, name).name
        p.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(store, name, p)
    rows = {r["key"]: r for r in store.data_freshness()}
    for k in ("prices", "flows", "short", "warnings", "market_flow", "consensus", "signal_hist"):
        assert rows[k]["counts_trading_days"] is True, f"{k}가 달력일로 센다 — 주말마다 오탐"
    for k in ("macro", "gurus"):
        assert rows[k]["counts_trading_days"] is False, f"{k}는 거래일과 무관한 소스다"


def test_weekend_padding_was_removed_not_kept(tmp_path, monkeypatch):
    """4일 버퍼는 **평일 3일 정지를 못 잡는** 우회였다 — 정확해졌으면 되돌려야 한다."""
    import ast
    import inspect
    src = inspect.getsource(store.data_freshness)
    tree = ast.parse(src.strip())
    thresholds = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "e" and len(node.args) >= 3:
            k = node.args[0]
            if isinstance(k, ast.Constant):
                thresholds[k.value] = node.args[3].value if len(node.args) > 3 else None
    for k in ("consensus", "signal_hist"):
        assert thresholds.get(k) == 2, (
            f"{k} 문턱이 {thresholds.get(k)} — 주말 버퍼가 남아 있으면 평일 정지를 못 잡는다")
