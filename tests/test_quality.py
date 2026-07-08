"""퀄리티 팩터(축약 F-Score) — 수익성·개선·건전성·성장 5점 + 엔진 반영."""

from signal_desk.signals import engine, quality


def test_full_five_points():
    cur = {"net_income": 100, "roe": 12.0, "debt_ratio": 80.0, "revenue_growth": 5.0}
    prev = {"roe": 10.0, "debt_ratio": 90.0}   # ROE↑, 부채↓
    q = quality.evaluate(cur, prev)
    assert q["points"] == 5 and q["has"] and len(q["checks"]) == 5


def test_zero_points():
    cur = {"net_income": -50, "roe": -3.0, "debt_ratio": 120.0, "revenue_growth": -10.0}
    prev = {"roe": -1.0, "debt_ratio": 100.0}  # ROE 악화, 부채 증가
    q = quality.evaluate(cur, prev)
    assert q["points"] == 0


def test_has_false_without_data():
    assert quality.evaluate({}, {})["has"] is False        # 근거 부족 → 제외 대상


def test_component_norm_and_excluded():
    n, w, r, pts, has = quality.component({"quality": {"points": 5, "max": 5, "checks": ["순이익 흑자"], "has": True}}, 0.15)
    assert has and n == 1.0 and w == 0.15 and pts == 5 and "[퀄리티]" in r[0]
    n0, w0, r0, pts0, has0 = quality.component({}, 0.15)   # quality 없음
    assert not has0 and w0 == 0.0


def test_evaluate_includes_quality():
    uni = [{"ticker": "005930", "name": "삼성전자"}]
    prices = {"005930": [100.0] * 60}
    fund = {"005930": {"quality": {"points": 4, "max": 5, "checks": ["순이익 흑자", "ROE 양(+)"], "has": True}}}
    r = engine.evaluate(uni, prices, fundamentals=fund)[0]
    assert r.has_quality and r.quality_points == 4 and any("[퀄리티]" in x for x in r.reasons)
    r2 = engine.evaluate(uni, prices)[0]
    assert r2.has_quality is False and r2.quality_points is None
