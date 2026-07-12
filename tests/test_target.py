"""참고 목표가 — 밸류 정상화(PER 회귀) + 기술적 저항."""

from signal_desk.signals import target


def test_median_per_needs_min_sample():
    assert target.median_per({"A": {"per": 10}}) is None            # 표본 부족(<5)
    fund = {t: {"per": p} for t, p in zip("ABCDE", [8, 10, 12, 14, 100])}
    assert target.median_per(fund) == 12.0                          # 적자/None·음수 제외한 중앙값


def test_value_target_and_clamp():
    # 저PER(10) < 중앙값(20) → 정상화 목표가 2배지만 상한 클램프(×2.0)
    t = target.compute(price=1000.0, per=10.0, med_per=20.0, closes=None)
    assert t["value_target"] == 2000 and t["value_upside_pct"] == 100.0
    # 고PER(40) > 중앙값(20) → 하향 목표(×0.5) 클램프
    t2 = target.compute(price=1000.0, per=40.0, med_per=20.0, closes=None)
    assert t2["value_target"] == 500 and t2["value_upside_pct"] == -50.0


def test_technical_resistance():
    closes = [90.0] * 40 + [100.0, 120.0, 110.0]   # 최근 고점 120
    t = target.compute(price=110.0, per=None, med_per=None, closes=closes)
    assert t["resistance"] == 120 and t["resistance_upside_pct"] == round((120/110-1)*100, 1)


def test_sector_median_per():
    fund = {f"X{i}": {"per": p} for i, p in enumerate([8, 10, 12, 14, 200])}  # 섹터X 5종목
    fund.update({f"Y{i}": {"per": p} for i, p in enumerate([30, 40])})         # 섹터Y 2종목(<min)
    secmap = {**{f"X{i}": "X" for i in range(5)}, **{f"Y{i}": "Y" for i in range(2)}}
    med = target.sector_median_per(fund, secmap, min_n=5)
    assert med.get("X") == 12.0        # 섹터X 중앙값(적자·이상치 무관하게 중앙)
    assert "Y" not in med              # 표본 부족(2<5) → 제외 → 호출부가 유니버스 fallback


def test_fwd_value_anchor():
    # 선행EPS 120 × 중앙값 PER 10 = 1200 (현재가 1000 대비 +20%)
    t = target.compute(price=1000.0, per=None, med_per=10.0, closes=None, fwd_eps=120.0)
    assert t["fwd_value_target"] == 1200 and t["fwd_value_upside_pct"] == 20.0


def test_fwd_value_skipped_on_nonpositive_eps():
    # 적자 컨센서스(선행EPS<=0)는 PER 밸류 불가 → 앵커 없음
    t = target.compute(price=1000.0, per=None, med_per=10.0, closes=None, fwd_eps=-50.0)
    assert t is None or "fwd_value_target" not in t


def test_analyst_anchor_wider_clamp():
    # 애널 목표가는 0.5~3.0배 클램프 → 4배 제시도 3배로 상한
    t = target.compute(price=1000.0, per=None, med_per=None, closes=None, analyst_target=4000.0)
    assert t["analyst_target"] == 3000 and t["analyst_upside_pct"] == 200.0
    # 정상 범위(1500)는 그대로
    t2 = target.compute(price=1000.0, per=None, med_per=None, closes=None, analyst_target=1500.0)
    assert t2["analyst_target"] == 1500 and t2["analyst_upside_pct"] == 50.0


def test_v2_anchors_are_additive():
    # 후행·선행·애널·저항이 모두 있으면 넷 다 노출(하나로 뭉치지 않음)
    closes = [90.0] * 40 + [130.0]
    t = target.compute(price=1000.0, per=20.0, med_per=20.0, closes=closes,
                       analyst_target=1300.0, fwd_eps=60.0)
    assert {"value_target", "fwd_value_target", "analyst_target", "resistance"} <= set(t)


def test_none_when_no_data():
    assert target.compute(price=None, per=10, med_per=20, closes=None) is None
    assert target.compute(price=1000.0, per=None, med_per=None, closes=[1, 2]) is None  # 표본<20, PER없음
    # v2 앵커도 데이터 없으면 None
    assert target.compute(price=1000.0, per=None, med_per=None, closes=None,
                          analyst_target=None, fwd_eps=None) is None
