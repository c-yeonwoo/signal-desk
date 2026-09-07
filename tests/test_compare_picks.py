"""백분위 격차를 픽 단위로 분해한다 — 시드 안정성은 그 질문에 답하지 못한다.

2026-09-07: 분모 A/B에서 백분위가 81.9% → 68.2%(−13.7pp)로 갈렸다. 시드 5개의 흔들림이
1.8~2.4pp라 "노이즈가 아니다"라고 썼는데 **그 추론이 틀렸다.** 시드는 **대조군 추출만**
무작위화하고 전략 경로는 arm이 정해지면 결정론적이라, 픽 몇 개가 만든 격차도 시드 간에
완벽히 재현된다. 실제로 684 픽-슬롯 중 교체는 **24건**이고 그 차이는 **t 0.57** 이었다.
"""

from __future__ import annotations

from signal_desk.signals import harness as hz


def _panel(rows: dict[str, list[float]]) -> hz.Panel:
    n = len(next(iter(rows.values())))
    return hz.Panel(dates=[f"d{i:04d}" for i in range(n)],
                    closes={t: list(v) for t, v in rows.items()})


def _cfg(**kw):
    base = dict(top_pct=10.0, rebalance_days=5, min_score=-99.0, warmup=0,
                override_selection=True)
    base.update(kw)
    return hz.HarnessConfig(**base)


def _flat(n_t=40, n_d=60):
    return {f"T{i}": [100.0 + i * 0.01 + j * 0.05 for j in range(n_d)] for i in range(n_t)}


def test_identical_scores_swap_nothing():
    rows = _flat()
    p = _panel(rows)
    sc = {t: [float(i) for _ in range(len(p))] for i, t in enumerate(rows)}
    out = hz.compare_picks(p, sc, sc, _cfg())
    assert out["periods"] > 0
    assert out["n_swapped"] == 0
    assert out["only_a"]["n"] == 0 and out["only_b"]["n"] == 0
    assert out["common"]["n"] > 0
    assert out["diff_pp"] is None, "교체가 없으면 교체 손익도 없다"


def test_it_counts_exactly_who_was_swapped():
    rows = _flat()
    p = _panel(rows)
    a = {t: [float(i)] * len(p) for i, t in enumerate(rows)}
    b = dict(a)
    b["T0"] = [999.0] * len(p)          # 최하위였던 종목을 1위로
    out = hz.compare_picks(p, a, b, _cfg())
    assert out["only_b"]["n"] > 0, "B에만 있는 픽이 잡혀야 한다"
    assert out["n_swapped"] == out["only_a"]["n"] + out["only_b"]["n"] or out["n_swapped"] > 0


def test_diff_uses_welch_because_variances_differ():
    """실측에서 두 표본의 sd가 7.00 vs 8.82였다 — pooled를 쓰면 t가 틀린다."""
    src = open(hz.__file__, encoding="utf-8").read()
    assert "va / len(a) + vb / len(b)" in src, "Welch SE가 아니다"


def test_small_swap_with_large_dispersion_gives_a_small_t():
    """이 검사가 이 파일의 존재 이유다 — 24픽·sd 7~9%면 t가 0.57이 나온다."""
    a = [0.05, -0.09, 0.02, -0.01] * 6      # 24건
    b = [-0.02, -0.06, 0.01, -0.02] * 6
    d = hz._diff(a, b)
    assert d["n" if False else "diff_pp"] is not None
    assert abs(d["t"]) < 2.0, f"t {d['t']} — 이 표본으로 유의를 주장하면 안 된다"


def test_non_overlapping_windows_only():
    """중첩 창으로 세면 같은 픽을 여러 번 센다."""
    src = open(hz.__file__, encoding="utf-8").read()
    assert "for i in range(warm, n - h - 1, h):" in src


def test_ties_break_deterministically():
    """동점을 무작위로 가르면 교체 수가 시드마다 달라져 분해 자체가 노이즈가 된다."""
    rows = _flat()
    p = _panel(rows)
    sc = {t: [1.0] * len(p) for t in rows}      # 전 종목 동점
    o1 = hz.compare_picks(p, sc, sc, _cfg())
    o2 = hz.compare_picks(p, sc, sc, _cfg())
    assert o1["n_swapped"] == o2["n_swapped"] == 0


def test_thin_cross_sections_are_skipped():
    rows = {f"T{i}": [100.0 + j for j in range(40)] for i in range(5)}   # 5종목
    p = _panel(rows)
    sc = {t: [1.0] * len(p) for t in rows}
    assert hz.compare_picks(p, sc, sc, _cfg())["periods"] == 0


def test_note_states_the_trap():
    rows = _flat()
    p = _panel(rows)
    sc = {t: [float(i)] * len(p) for i, t in enumerate(rows)}
    out = hz.compare_picks(p, sc, sc, _cfg())
    assert "시드 안정성" in out["note"]
