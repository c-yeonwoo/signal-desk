"""국면 익스포저에 타이밍 능력이 있나 — 아무도 잰 적이 없었다.

하네스는 대조군에도 같은 익스포저를 건다(기계적 조건을 같게 하려고). 그래서 켜고 끄면
전략·대조군이 함께 줄고 백분위가 거의 안 변한다(실측 94.0 vs 94.5). 익스포저는 순위
판별력과 직교하는 사이징 레이어라 당연하다 — 즉 **하네스는 이 질문에 답하지 못한다.**

물어야 할 것은 "같은 평균 익스포저의 상수 전략보다 나은가"다.
"""

from __future__ import annotations

from signal_desk import store
from signal_desk.signals import regime


def _draw(rng, n, mode):
    """(익스포저, 다음기간 수익) 표본. mode: skill|inverted|null."""
    ex, fwd = [], []
    for _ in range(n):
        r = rng.gauss(0.0005, 0.02)                 # 시장 수익
        if mode == "null":
            e = rng.choice([0.2, 0.4, 0.7, 1.0])    # 수익과 무관
        else:
            good = r > 0
            if mode == "inverted":
                good = not good
            # 능력이 완벽하진 않다 — 70%만 맞힌다
            if rng.random() > 0.7:
                good = not good
            e = rng.choice([0.7, 1.0]) if good else rng.choice([0.2, 0.4])
        ex.append(e)
        fwd.append(r)
    return ex, fwd


def test_false_positive_rate_is_near_nominal():
    """**검정을 새로 만들면 귀무 오탐률부터 잰다.** 안 재면 '아무것도 통과 못 하는 검사'와
    '잘 만든 검사'를 구분할 수 없다."""
    import random
    hits = 0
    trials = 300
    for seed in range(trials):
        rng = random.Random(seed)
        ex, fwd = _draw(rng, 250, "null")
        r = regime.timing_skill(ex, fwd)
        if r["verdict"] != "타이밍 능력 근거 없음(유의하지 않음)":
            hits += 1
    rate = hits / trials
    assert 0.01 <= rate <= 0.11, f"오탐률 {rate:.1%} (명목 5%) — 교정이 깨졌다"


def test_power_against_real_timing_skill():
    """대립가설 검출력 — 오탐률만 재고 검출력을 안 재면 통과 못 하는 검사가 된다."""
    import random
    hits = 0
    trials = 100
    for seed in range(1000, 1000 + trials):
        rng = random.Random(seed)
        ex, fwd = _draw(rng, 250, "skill")
        if regime.timing_skill(ex, fwd)["verdict"] == "타이밍 기여 있음":
            hits += 1
    assert hits / trials > 0.7, f"검출력 {hits / trials:.0%} — 진짜 능력을 못 잡는다"


def test_inverted_timing_is_called_harmful():
    import random
    hits = 0
    for seed in range(2000, 2100):
        rng = random.Random(seed)
        ex, fwd = _draw(rng, 250, "inverted")
        if regime.timing_skill(ex, fwd)["verdict"] == "타이밍이 오히려 해롭다":
            hits += 1
    assert hits / 100 > 0.7


def test_small_sample_says_why_not_zero():
    r = regime.timing_skill([0.7] * 10, [0.01] * 10)
    assert r["ready"] is False
    assert "기간 필요" in r["reason"]
    assert r["t"] is None, "요건 미달인데 숫자를 냈다"


def test_constant_exposure_has_exactly_zero_timing():
    r = regime.timing_skill([0.42] * 120, [0.003 * (1 if i % 3 else -2) for i in range(120)])
    assert abs(r["timing_pp"]) < 1e-9, "상수 익스포저는 정의상 타이밍 기여가 0이다"


def test_exposure_by_regime_orders_by_exposure():
    rows = regime.exposure_by_regime(["강세", "조정", "약세", "강세"],
                                     [0.001, 0.02, 0.005, 0.002])
    assert [r["regime"] for r in rows] == ["강세", "약세", "조정"]
    assert rows[0]["exposure"] == 1.0 and rows[-1]["exposure"] == 0.2
    assert rows[0]["n"] == 2


def test_snapshot_is_point_in_time(tmp_path, monkeypatch):
    """사후에 오늘의 유니버스로 과거 국면을 다시 매기면 그건 PIT가 아니다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import db
    db._CONN = None
    assert store.snapshot_regime("약세", 0.4, date="2026-09-01") is True
    assert store.snapshot_regime("중립", 0.7, date="2026-09-02") is True
    store.snapshot_regime("강세", 1.0, date="2026-09-02")      # 같은 날 덮어쓰기
    h = store.regime_history()
    assert [r["date"] for r in h] == ["2026-09-01", "2026-09-02"]
    assert h[-1]["regime"] == "강세" and h[-1]["exposure"] == 1.0


def test_snapshot_is_called_daily():
    """안 부르면 이력이 안 쌓이고 이 측정은 영영 못 한다."""
    src = open("src/signal_desk/api.py", encoding="utf-8").read() \
        if __import__("os").path.exists("src/signal_desk/api.py") \
        else open(__import__("signal_desk.api", fromlist=["x"]).__file__, encoding="utf-8").read()
    assert "store.snapshot_regime(" in src
    assert "regime_timing" in src, "재놓고 어디에도 안 내보낸다"


def test_capacity_declares_unreachable_slots():
    """게이트 이전에 이미 도달 불가능한 자리를 드러낸다(0의 이유 규칙)."""
    from signal_desk.signals import engine
    from signal_desk.signals.engine import SignalConfig
    cap = engine._capacity_summary(200, SignalConfig(rank_top_pct=3.0))
    assert cap["window_slots"] == 6
    cons = next(r for r in cap["styles"] if r["style"] == "conservative")
    assert cons["max_positions"] == 12 and cons["reachable"] == 6 and cons["short_by"] == 6
    assert cap["capped"] is True
    assert "도달 불가" in cap["note"]


def test_capacity_is_silent_when_the_window_is_big_enough():
    from signal_desk.signals import engine
    from signal_desk.signals.engine import SignalConfig
    cap = engine._capacity_summary(1000, SignalConfig(rank_top_pct=3.0))   # 창 30자리
    assert cap["capped"] is False and cap["note"] is None


def test_selection_summary_carries_capacity():
    from signal_desk.signals import engine
    src = open(engine.__file__, encoding="utf-8").read()
    assert '"capacity": _capacity_summary(' in src
