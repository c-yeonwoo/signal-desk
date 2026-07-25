"""advisor shadow — LLM 선별 vs 점수순 폴백 기록·채점(관측 전용)."""

import pytest

from signal_desk import store
from signal_desk.signals import advisor_shadow


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    return tmp_path


def _closes(start=100.0, n=20, step=1.0):
    dates = [f"2026-01-{d:02d}" for d in range(1, n + 1)]
    return dates, [start + step * i for i in range(n)]


_POOL = [{"ticker": "DOWN", "score": 2.0}, {"ticker": "UP", "score": 1.9}]


def test_record_first_run_per_day_only():
    assert advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=None, slots=1,
                                 date="2026-01-01") is True
    # 같은 날 두 번째 회차는 잔여 슬롯 관측이라 표본을 편향시킴 → 무시
    assert advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=None, slots=1,
                                 date="2026-01-01") is False
    # 다른 유저·다른 시장은 별개 표본
    assert advisor_shadow.record(uid=2, market="kr", pool=_POOL, picks=None, slots=1,
                                 date="2026-01-01") is True
    assert advisor_shadow.record(uid=1, market="us", pool=_POOL, picks=None, slots=1,
                                 date="2026-01-01") is True


def test_record_skips_empty_pool_or_no_slots():
    assert advisor_shadow.record(uid=1, market="kr", pool=[], picks=None, slots=2) is False
    assert advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=None, slots=0) is False


def test_divergent_names_only_are_scored():
    # 점수순이면 DOWN(2.0)을 사지만, LLM은 UP(1.9)을 골랐다 → 갈린 한 쌍만 채점
    advisor_shadow.record(uid=1, market="kr", pool=_POOL, slots=1,
                          picks=[{"ticker": "UP", "rationale": "x"}], date="2026-01-01")
    up_d, up_c = _closes(step=1.0)
    dn_d, dn_c = _closes(step=-1.0)
    out = advisor_shadow.summary({"UP": (up_d, up_c), "DOWN": (dn_d, dn_c)}, horizon=5)
    assert out["ready"] is True
    assert out["divergent_runs"] == 1 and out["advisor_used_runs"] == 1
    assert out["llm_only"]["n"] == 1 and out["llm_only"]["avg_ret_pct"] > 0
    assert out["base_only"]["n"] == 1 and out["base_only"]["avg_ret_pct"] < 0
    assert out["delta_pct"] > 0
    # 표본 1쌍으로 우열을 말하지 않는다
    assert out["verdict_ready"] is False
    assert "미변경" in out["disclaimer"]


def test_overlapping_picks_leave_nothing_to_score():
    pool = [{"ticker": "UP", "score": 2.0}]
    advisor_shadow.record(uid=1, market="kr", pool=pool, slots=1,
                          picks=[{"ticker": "UP", "rationale": "x"}], date="2026-01-01")
    up_d, up_c = _closes(step=1.0)
    out = advisor_shadow.summary({"UP": (up_d, up_c)}, horizon=5)
    assert out["divergent_runs"] == 0
    assert out["llm_only"]["n"] == 0 and out["base_only"]["n"] == 0
    assert out["delta_pct"] is None
    # 겹쳐도 양쪽 전체 수익은 기록된다(동률 확인용)
    assert out["llm_all"]["avg_ret_pct"] == out["baseline_all"]["avg_ret_pct"]


def test_fallback_runs_counted_but_not_compared():
    advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=None, slots=1, date="2026-01-01")
    out = advisor_shadow.summary({}, horizon=5)
    assert out["runs"] == 1 and out["advisor_used_runs"] == 0
    assert out["ready"] is False


def test_summary_without_records():
    out = advisor_shadow.summary({})
    assert out["ready"] is False and out["days"] == []
