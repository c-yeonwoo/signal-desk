"""성장 팩터 — 하네스 전용 노브. 라이브 점수는 한 자리도 바뀌면 안 된다.

매출성장은 이미 `fundamental` 안에 ±0.7짜리 계단 하나로 들어 있다(총 ±2.0 중). 그래서
자기 가중치를 갖지 못하고 PER·PBR·ROE와 한 덩어리로 묶여 있다. 이 파일은 그걸 **독립
팩터로 분리했을 때** 판별력이 달라지는지 재기 위한 장치가 제대로 붙었는지 검사한다.
"""

from __future__ import annotations

from signal_desk.signals import fundamental as fnd
from signal_desk.signals import growth
from signal_desk.signals import pit_fundamentals as pf
from signal_desk.signals.engine import SignalConfig


def _m(**kw):
    return dict(kw)


# ── 라이브 불변 (가장 중요) ──────────────────────────────────────────────────
def test_fundamental_default_is_unchanged():
    """기본값이 바뀌면 사전등록된 판정이 무효가 된다."""
    m = _m(per=8.0, pbr=0.9, roe=18.0, revenue_growth=22.0, debt_ratio=80.0)
    assert fnd.score(m).score == fnd.score(m, include_growth=True).score


def test_growth_term_is_worth_exactly_what_the_step_says():
    m = _m(per=30.0, pbr=5.0, roe=18.0, revenue_growth=22.0)   # 클램프에 안 닿는 조합
    with_g = fnd.score(m).score
    without = fnd.score(m, include_growth=False).score
    assert round(with_g - without, 6) == 0.7, "고성장 계단은 +0.7이어야 한다"


def test_the_clamp_can_swallow_the_growth_term_entirely():
    """±2.0 클램프 때문에 이미 강한 종목은 **성장이 0을 기여**한다.

    PER 8(+0.7) · PBR 0.9(+0.5) · ROE 18(+1.0) = 2.2 → 클램프 2.0. 여기에 고성장 +0.7을
    더해도 여전히 2.0이다. 성장이 `fundamental` 안에 묶여 있는 한, 정작 성장이 중요한
    구간에서 성장이 사라지는 경우가 생긴다 — 독립 팩터로 분리해 재보는 이유 중 하나다.
    """
    m = _m(per=8.0, pbr=0.9, roe=18.0, revenue_growth=22.0)
    assert fnd.score(m).score == fnd.score(m, include_growth=False).score == 2.0


def test_excluding_growth_keeps_has_data():
    """성장만 있는 종목이 include_growth=False에서 '재무 없음'이 되면 안 된다 —
    그러면 그 종목이 통째로 가중 0으로 빠져 arm 간 모집단이 달라진다."""
    r = fnd.score(_m(revenue_growth=30.0), include_growth=False)
    assert r.has_data is True
    assert r.score == 0.0


def test_pit_components_default_has_no_growth():
    comps = pf.components_at("A", _m(per=8.0, pbr=0.9, roe=18.0, revenue_growth=22.0),
                             {"A": 20.0}, SignalConfig())
    assert len(comps) == 3, "기본에서 성장 컴포넌트가 붙으면 라이브가 바뀐다"


def test_harness_default_growth_weight_is_zero():
    import inspect
    from signal_desk.signals import harness as hz
    sig = inspect.signature(hz.scores_with_pit_fundamentals)
    assert sig.parameters["growth_weight"].default == 0.0


# ── 팩터 자체 ────────────────────────────────────────────────────────────────
def test_percentile_excludes_missing_rather_than_zero_filling():
    """'성장을 모른다'를 '성장이 중간이다'로 번역하면 안 된다."""
    p = growth.percentile_scores({"A": _m(revenue_growth=10), "B": _m(revenue_growth=-5),
                                  "C": _m(), "D": _m(revenue_growth=None)})
    assert set(p) == {"A", "B"}
    assert p["B"] == 0.0 and p["A"] == 100.0


def test_high_growth_is_positive():
    p = growth.percentile_scores({f"T{i}": _m(revenue_growth=i) for i in range(11)})
    hi, _, _ = growth.component("T10", p, 0.30)
    lo, _, _ = growth.component("T0", p, 0.30)
    mid, _, _ = growth.component("T5", p, 0.30)
    assert hi == 1.0 and lo == -1.0 and abs(mid) < 1e-9


def test_missing_ticker_gets_zero_weight():
    _n, w, _r = growth.component("ZZZ", {"A": 50.0}, 0.30)
    assert w == 0.0, "결측을 0점으로 넣으면 모르는 종목이 중립이 된다"


def test_zero_weight_disables_it():
    p = growth.percentile_scores({"A": _m(revenue_growth=99)})
    assert growth.component("A", p, 0.0) == (0.0, 0.0, [])


def test_percentile_convention_is_shared_with_valuation():
    """두 팩터가 다른 랭킹을 쓰면 분위의 뜻이 갈린다."""
    src = open(growth.__file__, encoding="utf-8").read()
    assert "from signal_desk.signals.valuation import _percentile_rank" in src


def test_outliers_do_not_dominate():
    """실측 매출성장 최대는 +1875%다. 절대값을 쓰면 그 하나가 분포를 먹는다."""
    p = growth.percentile_scores({"A": _m(revenue_growth=1875), "B": _m(revenue_growth=20),
                                  "C": _m(revenue_growth=19), "D": _m(revenue_growth=18)})
    assert p["A"] == 100.0 and p["B"] > p["C"] > p["D"]
    assert p["B"] - p["C"] == p["C"] - p["D"], "분위는 간격이 균등해야 한다(값의 크기 무관)"


def test_growth_scores_are_measured_per_date_in_the_harness():
    """전 기간 고정 분위를 쓰면 오늘의 성장률 순위를 과거에 알고 있었던 셈이다."""
    from signal_desk.signals import harness as hz
    src = open(hz.__file__, encoding="utf-8").read()
    assert "growth_scores = pf.growth_scores_at(metrics) if growth_weight else None" in src
