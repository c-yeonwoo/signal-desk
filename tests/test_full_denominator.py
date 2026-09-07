"""재정규화 편향 — 분모를 무엇으로 나눌 것인가.

`combine` 의 분모는 **발동한 가중치의 합**이라, 팩터가 빠진 종목이 남은 팩터로 재정규화돼
극단 점수를 받는다. CLAUDE.md가 "분모를 전체 가중합으로 바꾸는 것이 편향의 정직한 해법이지만
모든 점수가 변하므로 판별력 판정 전에는 하지 않고 **하네스에 넣어 재는 것이 먼저**"라고
적어 뒀는데 그 노브가 여태 없었다. 이 파일은 그 노브가 제대로 붙었는지 검사한다.
"""

from __future__ import annotations

from signal_desk.signals import engine
from signal_desk.signals.engine import SignalConfig

CFG = SignalConfig()


def _c(*pairs):
    return [(norm, w, []) for norm, w in pairs]


# ── 라이브 불변 (가장 중요) ──────────────────────────────────────────────────
def test_default_is_unchanged():
    comps = _c((1.0, 0.30), (0.0, 0.0), (-0.5, 0.15))
    assert engine.combine(comps, CFG)["score"] == engine.combine(comps, CFG, denominator=None)["score"]


def test_zero_denominator_falls_back_not_divides_by_zero():
    comps = _c((1.0, 0.30))
    assert engine.combine(comps, CFG, denominator=0.0)["score"] == engine.combine(comps, CFG)["score"]


def test_harness_default_is_off():
    from signal_desk.signals import harness as hz
    assert hz.HarnessConfig().full_denominator is False


# ── 편향 자체 ────────────────────────────────────────────────────────────────
def test_missing_factor_inflates_the_score_today():
    """모멘텀 하나만 발동한 종목이 만점을 받는다 — 미국 1·2위(VTRS·IRM)가 이 경우였다."""
    only_momentum = _c((1.0, 0.30))
    assert engine.combine(only_momentum, CFG)["score"] == 3.0


def test_neutral_denominator_shrinks_it_proportionally():
    only_momentum = _c((1.0, 0.30))
    den = engine.countable_weight(CFG, unavailable=("flow", "short"))   # 0.90
    out = engine.combine(only_momentum, CFG, denominator=den)
    assert round(out["score"], 2) == 1.0, "0.30/0.90 = 1/3 → 3.0 × 1/3"
    assert out["denominator"] == round(den, 4)


def test_full_coverage_is_untouched():
    """전 팩터를 가진 종목은 분모가 같으므로 점수가 안 변한다 — 실측 셀의 83%가 여기다."""
    comps = _c((0.5, 0.30), (0.2, 0.15), (-0.1, 0.15), (0.8, 0.30))     # 합 0.90
    den = engine.countable_weight(CFG, unavailable=("flow", "short"))
    assert engine.combine(comps, CFG)["score"] == engine.combine(comps, CFG, denominator=den)["score"]


# ── 분모를 무엇으로 세느냐 ───────────────────────────────────────────────────
def test_unavailable_factors_are_excluded():
    """원리적으로 못 보는 팩터를 분모에 넣으면 전 종목이 똑같이 축소된다 —
    그건 편향 제거가 아니라 스케일 축소다."""
    full = engine.total_weight(CFG)
    pit6 = engine.countable_weight(CFG, unavailable=("flow", "short"))
    assert pit6 < full
    assert round(full - pit6, 4) == round(CFG.weight_flow + CFG.weight_short + CFG.weight_reversion, 4)


def test_unfired_conditional_is_excluded_but_fired_is_counted():
    """낙폭과대는 급락 때만 발동한다(발동률 2.4%). 안 걸린 것을 결측으로 세면
    평상시 97.6%의 종목이 부당하게 깎인다."""
    off = engine.countable_weight(CFG, unavailable=("flow", "short"))
    on = engine.countable_weight(CFG, unavailable=("flow", "short"),
                                 fired_conditional=("reversion",))
    assert round(on - off, 4) == round(CFG.weight_reversion, 4)


def test_convention_matches_data_coverage():
    """분모와 커버리지 게이트가 다른 것을 세면 게이트가 엉뚱한 것을 막는다."""
    unavail = ("flow", "short")
    cov = engine.data_coverage({}, CFG, unavailable=unavail)
    assert cov["countable_weight"] == round(
        engine.countable_weight(CFG, unavailable=unavail), 4)


def test_harness_passes_the_right_unavailable_set():
    from signal_desk.signals import harness as hz
    src = open(hz.__file__, encoding="utf-8").read()
    assert "unavailable=_PRICE_UNAVAILABLE" in src and "unavailable=_PIT_UNAVAILABLE" in src
    assert 'fired_conditional=("reversion",) if comps[1][1] else ()' in src, \
        "조건부 팩터 발동 여부를 안 보면 평상시 종목이 부당하게 깎인다"


def test_registration_pins_the_denominator():
    """등록에서 빠진 파라미터는 검증된 적이 없다 — 같은 id가 두 전략을 재면 안 된다."""
    from signal_desk import prereg
    assert "full_denominator" in prereg._HARNESS_KEYS
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    for lk in reg["looks"]:
        assert lk["harness"]["full_denominator"] is False, (
            f"{lk['id']}: 분모 선언이 없거나 다르다")


def test_declaring_it_did_not_change_the_threshold():
    """값이 그대로면 n도 문턱도 안 움직여야 한다 — 선언은 사실을 적는 것이지 새 가설이 아니다."""
    from signal_desk import prereg
    reg = prereg.load()
    assert reg["n_looks_total"] == 6
    assert reg["threshold_pct"] == prereg.sidak_threshold_pct(6)


def test_run_preregistered_reads_it():
    from signal_desk import store
    src = open(store.__file__, encoding="utf-8").read()
    assert 'hzc.get("full_denominator")' in src
    assert "full_denominator=_full_den" in src
