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


def test_abstention_is_scored_as_cash():
    """기권은 '현금 보유'로 채점한다 — 비워두면 기권이 사라져 delta가 폴백에 유리해진다."""
    advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=[], slots=1, date="2026-01-01")
    dn_d, dn_c = _closes(step=-1.0)
    out = advisor_shadow.summary({"DOWN": (dn_d, dn_c)}, horizon=5)
    assert out["abstained_runs"] == 1 and out["advisor_used_runs"] == 1
    # 점수순은 DOWN을 샀고 그건 내렸다. 기권 쪽은 0% → delta 양수(기권이 옳았다)
    assert out["llm_only"] == {"n": 1, "avg_ret_pct": 0.0}
    assert out["base_only"]["avg_ret_pct"] < 0 and out["delta_pct"] > 0


def test_verdict_needs_significance_not_just_sample_count():
    """표본 20을 채워도 분산이 크면 부호를 읽지 않는다 — 20쌍 판정 착각 제거."""
    up_d, up_c = _closes(step=1.0)
    dn_d, dn_c = _closes(step=-1.0)
    closes = {"UP": (up_d, up_c), "DOWN": (dn_d, dn_c)}
    for i in range(1, 21):  # 유저 20명 × 갈린 1쌍 = 한쪽 20표본
        advisor_shadow.record(uid=i, market="kr", pool=_POOL, slots=1,
                              picks=[{"ticker": "UP", "rationale": "x"}], date="2026-01-01")
    out = advisor_shadow.summary(closes, horizon=5)
    assert out["matured_smaller_side"] == 20 and out["sample_target_reached"] is True
    # 같은 종목쌍만 반복돼 분산이 0 → 표준오차 0이라 유의. 이름은 pair가 아님을 명시
    assert out["delta_se_pp"] == 0.0 and out["delta_significant"] is True
    assert out["matured_pairs"] == out["matured_smaller_side"]
    assert out["paired_n"] == 20
    assert "판정 근거가 아니다" in out["verdict_note"]
    assert "paired_delta" in out["verdict_note"]


def test_summary_without_records():
    out = advisor_shadow.summary({})
    assert out["ready"] is False and out["days"] == []


def test_paired_delta_matches_rank_swap():
    """같은 회차에서 1위↔3위 교체면 paired_delta는 그 두 수익 차와 같다."""
    # pool: A(최고)·B·C(최저). slots=1 → baseline=A. LLM이 C를 고르면 갈린 한 쌍.
    pool = [{"ticker": "A", "score": 3.0}, {"ticker": "B", "score": 2.0},
            {"ticker": "C", "score": 1.0}]
    advisor_shadow.record(uid=1, market="kr", pool=pool, slots=1, style="balanced",
                          picks=[{"ticker": "C", "rationale": "x"}], date="2026-01-01")
    a_d, a_c = _closes(start=100, step=-1.0)   # A 하락
    c_d, c_c = _closes(start=100, step=2.0)    # C 상승
    b_d, b_c = _closes(start=100, step=0.0)
    out = advisor_shadow.summary(
        {"A": (a_d, a_c), "B": (b_d, b_c), "C": (c_d, c_c)}, horizon=5)
    assert out["paired_n"] == 1
    # unpaired와 paired가 같은 한 쌍이면 숫자도 같다(slots=1)
    assert out["paired_delta_pct"] == out["delta_pct"]
    assert out["paired_delta_pct"] > 0
    assert out["by_style"]["balanced"]["paired_n"] == 1
    assert out["by_style"]["balanced"]["paired_delta_pct"] == out["paired_delta_pct"]


def test_unpaired_penalizes_lower_rank_but_paired_is_fair():
    """unpaired는 '상위 vs 하위'라 구조적으로 불리하지만, paired는 자리끼리 맞춘다.

    같은 날 slots=2: baseline={TOP, MID}, LLM={MID, LOW}.
    갈림: base_only=TOP, llm_only=LOW → 한 쌍(1위 자리 vs LLM이 넣은 것).
    TOP이 조금 더 올라도(점수 예측력) unpaired·paired 숫자는 이 한 쌍에선 같다.
    구조 편향은 slots≥2에서 여러 갈림이 섞일 때 드러나므로, 여기서는
    'paired_n이 진짜 교체 쌍 수'이고 style이 기록되는지만 고정한다.
    """
    pool = [
        {"ticker": "TOP", "score": 3.0},
        {"ticker": "MID", "score": 2.0},
        {"ticker": "LOW", "score": 1.0},
    ]
    advisor_shadow.record(
        uid=900002, market="kr", pool=pool, slots=2, style="balanced",
        picks=[{"ticker": "MID", "rationale": "x"}, {"ticker": "LOW", "rationale": "y"}],
        date="2026-01-01",
    )
    # TOP +5%, LOW +1% — 상위가 더 올랐다
    def series(step):
        d, c = _closes(start=100, step=step)
        return d, c
    out = advisor_shadow.summary({
        "TOP": series(1.0), "MID": series(0.5), "LOW": series(0.2),
    }, horizon=5)
    assert out["paired_n"] == 1          # 교체 1자리(TOP↔LOW). MID는 겹쳐 상쇄
    assert out["llm_only"]["n"] == 1 and out["base_only"]["n"] == 1
    assert out["paired_delta_pct"] < 0   # LOW < TOP
    assert out["by_style"]["balanced"]["runs"] == 1
    # uid 폴백: style을 안 넣어도 레퍼런스 uid면 성향이 잡힌다
    advisor_shadow.record(
        uid=900001, market="kr", pool=pool, slots=1,
        picks=[{"ticker": "LOW", "rationale": "z"}], date="2026-01-02",
    )
    out2 = advisor_shadow.summary({
        "TOP": series(1.0), "MID": series(0.5), "LOW": series(0.2),
    }, horizon=5)
    assert "conservative" in out2["by_style"]


def test_record_stores_style():
    advisor_shadow.record(uid=1, market="kr", pool=_POOL, slots=1, style="aggressive",
                          picks=[{"ticker": "UP", "rationale": "x"}], date="2026-01-01")
    blob = advisor_shadow._load()
    assert blob["2026-01-01"][0]["style"] == "aggressive"


def test_abstention_pairs_cash_against_baseline():
    """기권 paired = 현금 0% − base_only 수익."""
    advisor_shadow.record(uid=1, market="kr", pool=_POOL, picks=[], slots=1,
                          style="balanced", date="2026-01-01")
    dn_d, dn_c = _closes(step=-1.0)
    out = advisor_shadow.summary({"DOWN": (dn_d, dn_c)}, horizon=5)
    assert out["paired_n"] == 1
    assert out["paired_delta_pct"] > 0   # 0 − (음수) = 양수
    assert out["paired_delta_pct"] == out["delta_pct"]
