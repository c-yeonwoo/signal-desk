"""미국 PIT 스냅샷 — 없으면 미국은 실측·IC·PIT 하네스를 영영 못 잰다.

**가장 위험한 것은 섞이는 것이다.** `accuracy.cross_sectional_ic` 는 날짜로 묶어 횡단면
순위상관을 내므로, 시장 컬럼 없이 미국 행을 같은 파일에 넣으면 그 날의 횡단면이 한·미
혼합이 된다 — 사전등록된 국내 IC look이 조용히 다른 것을 재게 된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from signal_desk import store


def _Sig(ticker, score=1.0):
    """스냅샷이 읽는 필드를 전부 가진 최소 시그널 — 엔진 dataclass를 그대로 쓴다.
    (직접 스텁을 만들면 필드가 늘 때마다 검사만 깨진다.)"""
    from signal_desk.signals.engine import SignalResult
    return SignalResult(ticker=ticker, name=ticker, score=score, kind="BUY",
                        confidence=0.5, technical_score=0.0, fundamental_score=0.0,
                        has_fundamental=False)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    return tmp_path


def test_kr_is_the_default_and_us_is_invisible_to_old_callers(fresh):
    store.snapshot_signals([_Sig("005930"), _Sig("000660")], date="2026-09-07")
    store.snapshot_signals([_Sig("AAPL"), _Sig("MSFT")], date="2026-09-07", market="us")
    kr = store.load_signal_history()
    assert sorted(kr["ticker"]) == ["000660", "005930"], \
        "기본 로더가 미국 행을 돌려주면 국내 IC 횡단면이 오염된다"
    us = store.load_signal_history("us")
    assert sorted(us["ticker"]) == ["AAPL", "MSFT"]
    both = store.load_signal_history(None)
    assert len(both) == 4


def test_same_day_rerun_only_replaces_its_own_market(fresh):
    store.snapshot_signals([_Sig("005930")], date="2026-09-07")
    store.snapshot_signals([_Sig("AAPL")], date="2026-09-07", market="us")
    store.snapshot_signals([_Sig("005930"), _Sig("000660")], date="2026-09-07")  # 국내 재실행
    assert len(store.load_signal_history("us")) == 1, "국내 재실행이 미국 행을 날렸다"
    assert len(store.load_signal_history()) == 2


def test_legacy_rows_without_market_are_domestic(fresh):
    """미국을 찍기 전 행에는 컬럼이 없다 — 그때는 국내만 찍었다."""
    store._write_parquet(pd.DataFrame([{"date": "2026-07-01", "ticker": "005930", "score": 1.0}]),
                         store.SIGNAL_HISTORY_FILE)
    assert len(store.load_signal_history()) == 1
    assert store.load_signal_history("us").empty


def test_pit_dates_count_is_per_market(fresh):
    store.snapshot_signals([_Sig("005930")], date="2026-09-07")
    store.snapshot_signals([_Sig("005930")], date="2026-09-08")
    store.snapshot_signals([_Sig("AAPL")], date="2026-09-08", market="us")
    assert store.pit_dates_count() == 2
    assert store.pit_dates_count(market="us") == 1


def test_pre_run_up_uses_the_right_market_prices(fresh):
    """미국 종목의 사전 상승을 국내 시세에서 찾으면 전부 결측이 된다."""
    src = open(store.__file__, encoding="utf-8").read()
    assert "load_us_price_series() if market == \"us\" else load_price_series()" in src


def test_daily_loop_snapshots_us_and_isolates_failures():
    src = open("src/signal_desk/api.py", encoding="utf-8").read() \
        if __import__("os").path.exists("src/signal_desk/api.py") else ""
    assert 'market="us"' in src, "일일 루프가 미국을 안 찍으면 데이터가 안 쌓인다"
    assert src.count("미국 시그널 스냅샷 실패") == 1, "미국 실패가 격리돼야 한다"
    assert src.count("국면 스냅샷 실패") == 1, "국면 스냅샷이 미국 실패에 딸려 죽으면 안 된다"
