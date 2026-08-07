"""목표금액 플랜 — 방법론 속성을 못박는다.

이 모듈이 틀리면 "얼마 더 넣어야 하나"라는 **행동을 바꾸는 숫자**가 틀린다. 그래서 결과보다
성질을 검사한다: 역산이 정확한가 · 세금이 실제로 깎이는가 · 가정을 지어내지 않는가.
"""

from __future__ import annotations

import numpy as np

from signal_desk.signals import goal_plan as gp


def _fixture(seed: int = 7, n: int = 400):
    rng = np.random.default_rng(seed)
    px1 = 100 * np.cumprod(1 + rng.normal(0.0004, 0.012, n))
    px2 = 50 * np.cumprod(1 + rng.normal(0.0003, 0.010, n))
    return ([{"ticker": "A", "qty": 100}, {"ticker": "B", "qty": 200}],
            {"A": list(px1), "B": list(px2)})


def test_required_contribution_is_exact_not_approximate():
    """역산은 **아핀 구조**를 이용하므로 근사가 아니라 정확해야 한다.

    필요 적립액을 그대로 넣으면 그 분위의 기간말 자산이 목표와 같아야 한다.
    이분탐색으로 짰다면 오차가 남고, 그 오차가 "월 얼마 더"라는 조언을 흔든다.
    """
    hold, prices = _fixture()
    goal, months = 50_000_000, 60
    base = gp.plan(hold, prices, goal_amount=goal, months=months, monthly_contribution=0,
                   div_yield_annual=0.032, sims=3000)
    assert base["ready"]
    for p in (10, 50, 90):
        need = base["required_monthly"][f"p{p}"]
        assert need and need > 0
        got = gp.plan(hold, prices, goal_amount=goal, months=months,
                      monthly_contribution=need, div_yield_annual=0.032,
                      sims=3000)["terminal"][f"p{p}"]
        assert abs(got - goal) / goal < 0.001, f"p{p}: {got} != {goal} — 역산이 정확하지 않다"


def test_dividend_tax_lowers_the_path():
    """세전 배당으로 재투자하면 경로가 낙관적으로 부풀려진다.

    국내(15.4%) 경로는 미국(15%) 경로보다 **낮아야** 한다 — 같은 배당수익률이라면.
    """
    hold, prices = _fixture()
    kw = dict(goal_amount=1, months=120, monthly_contribution=0, div_yield_annual=0.04, sims=2500)
    kr = gp.plan(hold, prices, currency="KRW", **kw)["terminal"]["p50"]
    us = gp.plan(hold, prices, currency="USD", **kw)["terminal"]["p50"]
    none_ = gp.plan(hold, prices, currency="KRW", **{**kw, "div_yield_annual": 0.0})["terminal"]["p50"]
    assert none_ < kr < us, f"세율 순서가 경로에 반영되지 않았다: 무배당 {none_} · KR {kr} · US {us}"
    assert gp.DIV_TAX["KRW"] > gp.DIV_TAX["USD"]


def test_more_contribution_never_lowers_the_path():
    """단조성 — 더 넣으면 경로가 낮아질 수 없다. 계수 누적이 뒤집히면 여기서 걸린다."""
    hold, prices = _fixture()
    prev = None
    for c in (0, 100_000, 500_000, 1_000_000):
        v = gp.plan(hold, prices, goal_amount=1, months=60, monthly_contribution=c,
                    sims=1500)["terminal"]["p50"]
        if prev is not None:
            assert v >= prev, f"적립 {c}에서 경로가 낮아졌다"
        prev = v


def test_success_probability_agrees_with_the_median_path():
    """`success_pct`와 p50이 어긋나면 둘 중 하나가 틀렸다.

    p50이 목표를 넘으면 달성확률은 50% 이상이어야 한다(정의상). 두 숫자가 갈라지면
    화면에서 **관대한 쪽이 읽힌다** — DSR 0.979 vs 백분위 71.5 때와 같은 병이다.
    """
    hold, prices = _fixture()
    for goal in (1_000_000, 20_000_000, 50_000_000, 500_000_000):
        r = gp.plan(hold, prices, goal_amount=goal, months=60,
                    monthly_contribution=300_000, sims=3000)
        over = r["terminal"]["p50"] >= r["goal_amount"]
        assert (r["success_pct"] >= 50.0) == over, (
            f"목표 {goal}: p50={r['terminal']['p50']} 인데 달성확률 {r['success_pct']}%")


def test_output_states_its_basis_and_inherited_bias():
    """경로를 내면서 **무엇을 근거로 만들었는지**와 물려받은 편향을 밝혀야 한다.

    이 문장이 없으면 '예측'으로 읽힌다. 부트스트랩은 보유종목의 과거를 그대로 물려받으므로
    승자를 들고 있으면 경로도 낙관적으로 나온다 — 하네스가 경계하는 생존편향과 같은 족속이다.
    """
    hold, prices = _fixture()
    r = gp.plan(hold, prices, goal_amount=1, months=12, sims=500)
    assert "부트스트랩" in r["basis"] and "가정하지 않" in r["basis"]
    assert "과거를 물려받" in r["caveat"]
    for word in ("배당 성장", "환율", "금융소득종합과세"):
        assert word in r["caveat"], f"{word} 를 반영하지 않았다는 사실을 밝히지 않는다"


def test_facts_never_claim_a_stock_will_go_up():
    """판별력이 판정 보류인 동안 '오를 종목'을 말하지 않는다 — 검증 불필요한 사실만."""
    items = [{"currency": "KRW", "name": "A", "annual": 800.0, "div_months": [4]},
             {"currency": "KRW", "name": "B", "annual": 200.0, "div_months": [4]}]
    fs = gp.facts(items, "KRW", plan_result={"ready": True, "gap_monthly": {"p50": 1000.0, "p10": 5000.0}})
    txt = " ".join(f["text"] for f in fs)
    for banned in ("오를", "상승할", "추천", "유망"):
        assert banned not in txt, f"미검증 주장이 섞였다: {banned}"
    kinds = {f["kind"] for f in fs}
    assert "div_concentration" in kinds, "배당 집중도(80%)를 잡지 않았다"
    assert "payout_gap" in kinds, "지급월 공백을 잡지 않았다"
    # 금액에 **단위**가 붙어야 한다 — `월 9,242 더` 는 원인지 달러인지 알 수 없다.
    assert "원" in txt
    fs_us = gp.facts([], "USD", plan_result={"ready": True, "gap_monthly": {"p50": 10.0, "p10": 20.0}})
    assert "$" in " ".join(f["text"] for f in fs_us)


def test_missing_dividend_data_is_not_silently_zero():
    """배당수익률 0%가 '배당 안 주는 종목'인지 '데이터 미수집'인지 구분해야 한다."""
    from pathlib import Path
    src = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    blk = src.split('def goal_plan_post(', 1)[1].split("\n@app.", 1)[0]
    assert "dividend_coverage" in blk
    assert "missing_dps" in blk and "수집되지 않은" in blk
