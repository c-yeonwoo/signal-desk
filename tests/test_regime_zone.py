"""시장 국면 체온계(regime_zone.assess) — ZONE 판정·회복 체크리스트·궤적 감지."""

from signal_desk.signals import regime_zone


def _uptrend(n=130, base=100.0):
    return [base + i for i in range(n)]


def _crash(n=130):
    # 앞 110봉 상승 → 최근 20봉 급락(저변이 위→아래로 무너지는 조정 심화 형태)
    rise = [100.0 + i for i in range(110)]
    drop = [210.0 - 8 * j for j in range(1, n - 110 + 1)]
    return rise + drop


def test_zone_strong_when_broad_uptrend():
    prices = {f"T{i}": _uptrend() for i in range(20)}
    snap = regime_zone.assess(prices, index_closes=_uptrend())
    assert snap["ready"]
    assert snap["zone"] in ("강세", "정상")
    assert snap["breadth_pct"] >= 90 and snap["score"] >= 70


def test_zone_correction_when_breadth_collapses():
    prices = {f"T{i}": _crash() for i in range(20)}
    snap = regime_zone.assess(prices, index_closes=_crash())
    assert snap["zone"] == "조정 심화"           # 저변이 위→아래로 무너짐(b_chg 크게 음)
    assert snap["recovery"]["met"] <= 1
    assert any(i["key"] == "trend" and i["state"] == "warn" for i in snap["indicators"])


def test_not_ready_on_short_data():
    snap = regime_zone.assess({"T": [1, 2, 3]}, index_closes=[1, 2, 3])
    assert snap["ready"] is False and snap["zone"] is None
