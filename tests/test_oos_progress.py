"""OOS look의 진척은 **자기 창 안의 날짜만** 세야 한다.

2026-09-06 진단: 보드가 `pit_dates` 를 한 번 계산해 **모든 look에 같은 값으로** 넘겼다.
그래서 `sigma-exits-oos`(창이 다음 날 열린다)의 진척이 `42/150일`로 떴다 — 창이 열리기도
전에 42일이 쌓여 있는 셈이다. 그대로 두면 요건이 **OOS 창 밖 데이터로** 채워져
"아직 보지 않은 구간에서 판정한다"는 전제가 거짓이 되고, OOS를 건 이유 전체가 무효가 된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from signal_desk import store


@pytest.fixture
def snap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    rows = []
    for d in ("2026-07-01", "2026-07-02", "2026-08-10", "2026-08-11", "2026-09-01"):
        for t in ("A", "B"):                       # 같은 날 2종목 = 하나의 관측
            rows.append({"date": d, "ticker": t, "score": 1.0})
    store._write_parquet(pd.DataFrame(rows), store.SIGNAL_HISTORY_FILE)
    store.load_signal_history.cache_clear() if hasattr(
        store.load_signal_history, "cache_clear") else None
    return tmp_path


def test_counts_dates_not_rows(snap):
    assert store.pit_dates_count() == 5, "행으로 세면 하루 2종목이 2관측으로 부푼다"


def test_from_date_narrows_the_count(snap):
    assert store.pit_dates_count("2026-08-01") == 3
    assert store.pit_dates_count("2026-09-01") == 1
    assert store.pit_dates_count("2026-12-31") == 0, "창이 아직 안 열렸으면 0이어야 한다"


def test_boundary_is_inclusive(snap):
    """`from_date` 당일은 포함한다 — 등록은 '그 날짜부터'다."""
    assert store.pit_dates_count("2026-08-10") == 3
    assert store.pit_dates_count("2026-08-11") == 2


def test_board_uses_each_looks_own_window():
    src = open(store.__file__, encoding="utf-8").read()
    assert "lk_pit = pit_dates_count(oos_from) if oos_from else pit_dates" in src, \
        "보드가 look마다 자기 창으로 세지 않는다"
    assert "prereg.progress(lk, effective_periods=eff, pit_dates=lk_pit)" in src


def test_board_shows_why_the_numerators_differ():
    """두 look의 분자가 다른 이유가 안 보이면 '왜 얘만 느리지'로 읽힌다."""
    src = open(store.__file__, encoding="utf-8").read()
    assert 'prog["counts_from"]' in src and 'prog["pit_dates_all"]' in src


def test_effective_periods_estimate_also_uses_the_window():
    """추정 실효기간이 전체 날짜로 계산되면 분자만 고치고 분모가 남는다."""
    src = open(store.__file__, encoding="utf-8").read()
    assert 'eff, eff_src = lk_pit // max(1, hold), "estimated"' in src


def test_run_preregistered_uses_the_sliced_count():
    """price6 경로의 `pit_dates` 는 자르기 전 값이라 OOS에 쓰면 안 된다."""
    src = open(store.__file__, encoding="utf-8").read()
    assert 'pre = pit_dates_count(_from_d) if pit else None' in src
    assert 'out.get("oos_dates") if _from_d else out.get("pit_dates")' in src
