"""시총 하한을 하네스에서 잰다 — 라이브에 넣기 전에.

「미국 쪽 시그널이 잡주 같다」에서 나온 세 갈래 중 하나. 규모 기준을 정말 올리고 싶다면
**먼저 재야 한다** — 판별력이 `판정 보류`인 동안 파라미터를 만지지 않는 것이 이 리포의 규칙이고,
측정되지 않은 불편함으로 후보를 좁히면 곡선 맞추기다.

이 리포가 게이트에서 세 번 겪은 함정을 그대로 검사한다.

1. **게이트를 만들면 하네스에도 넣는다.** 라이브에만 걸고 하네스에 안 걸면 하네스는 라이브가
   돌리지 않는 전략을 잰다.
2. **막힌 자리는 공석으로 둔다.** 다음 순위로 채우면 게이트가 아니라 재정렬이다 — 실측에서
   추세 게이트가 1076회 걸렸는데 켜고 끈 결과가 완전히 같았다.
3. **대조군에도 같은 패널을 넘긴다.** 시총은 티커에 붙은 사실이라 라벨 치환과 무관하다.
   대조군만 게이트를 안 걸면 대조군이 더 많이 살 수 있어 그 차이가 판별력으로 둔갑한다.

그리고 문턱은 **절대 금액이 아니라 그 날의 횡단면 분위**다 — 전 기간 고정 금액은 지수가 오른
구간에서 저절로 느슨해지고 내린 구간에서 저절로 조여진다(시총 규칙이 아니라 시장 수준 규칙).
"""

from __future__ import annotations

import inspect

from signal_desk.signals import harness as hz
from signal_desk.signals.engine import SignalConfig


def _panel(n_tickers: int = 12, n_days: int = 400):
    """가격은 전 종목 동일 — 수익률 차이를 없애 **게이트 효과만** 남긴다."""
    dates = [f"2025-{(i // 21) % 12 + 1:02d}-{i % 21 + 1:02d}" for i in range(n_days)]
    closes = {f"T{i:02d}": [100.0 + d * 0.1 for d in range(n_days)] for i in range(n_tickers)}
    return hz.Panel(dates=dates, closes=closes)


def _scores(panel, order):
    """order 앞쪽 티커가 높은 점수 — 순위를 결정론적으로 고정한다."""
    return {t: [float(len(order) - order.index(t))] * len(panel.dates) for t in order}


def _caps(panel, small: set[str]):
    return {t: [(1e9 if t in small else 1e13)] * len(panel.dates) for t in panel.closes}


def _cfg(**kw):
    return hz.HarnessConfig(top_pct=50.0, rebalance_days=5, cost_pct=0.0, warmup=10,
                            random_trials=10, phase_average=False, min_periods=1,
                            signal_config=SignalConfig(), override_selection=True,
                            min_score=-99.0, **kw)


def test_gate_blocks_and_reports_how_many():
    """**몇 번 막았는지**를 낸다. `걸었다`만으로는 효과 없는 완화를 구분할 수 없다."""
    panel = _panel()
    order = sorted(panel.closes)
    scores = _scores(panel, order)
    small = set(order[:3])                      # 점수 최상위 3종목이 소형주
    out = hz.run(panel, _cfg(min_mktcap_pct=30.0), scores=scores,
                 caps=_caps(panel, small), score_source="external")
    assert out["mktcap_gate"]["blocked"] > 0, "게이트가 한 번도 안 걸렸다 — 잰 것은 게이트 없는 전략"
    assert out["mktcap_gate"]["panel_given"] is True


def test_gate_without_a_panel_is_reported_not_silently_skipped():
    """패널 없이 켜면 **아무 것도 안 막는다** — 조용히 넘기면 무엇을 쟀는지 알 수 없다."""
    panel = _panel()
    order = sorted(panel.closes)
    out = hz.run(panel, _cfg(min_mktcap_pct=30.0), scores=_scores(panel, order),
                 score_source="external")
    assert out["mktcap_gate"]["blocked"] == 0 and out["mktcap_gate"]["panel_given"] is False
    assert any("시총 게이트" in w and "패널이" in w for w in out["warnings"]), out["warnings"]


def test_gate_that_never_fires_is_called_out():
    """효과 없는 완화는 있는 것처럼 보인다 — 실측에서 커버리지 0.75가 그랬다(4종목만 막음)."""
    panel = _panel()
    order = sorted(panel.closes)
    out = hz.run(panel, _cfg(min_mktcap_pct=30.0), scores=_scores(panel, order),
                 caps=_caps(panel, set()), score_source="external")   # 전부 대형주
    assert out["mktcap_gate"]["blocked"] == 0
    assert any("한 번도 막지 않았다" in w for w in out["warnings"]), out["warnings"]


def test_blocked_slot_stays_empty_instead_of_being_refilled():
    """**게이트가 재정렬이 되면 안 된다.** 막힌 자리를 k 밖에서 채우면 보유 수가 안 줄어든다."""
    panel = _panel()
    order = sorted(panel.closes)
    scores = _scores(panel, order)
    caps = _caps(panel, set(order[:3]))
    off = hz.run(panel, _cfg(min_mktcap_pct=0.0), scores=scores, caps=caps, score_source="external")
    on = hz.run(panel, _cfg(min_mktcap_pct=30.0), scores=scores, caps=caps, score_source="external")
    assert on["strategy"]["avg_picks"] < off["strategy"]["avg_picks"], \
        f"게이트를 켰는데 평균 보유 수가 그대로다 — 재정렬이다"


def test_control_group_gets_the_same_panel():
    """대조군만 게이트를 안 걸면 대조군이 더 많이 살 수 있고 그 차이가 판별력이 된다."""
    src = inspect.getsource(hz._null_distribution)
    assert "caps" in src.split("def _null_distribution")[-1].split('"""')[0], \
        "대조군이 시총 패널을 안 받는다"
    assert "random.Random(cfg.seed), covers, caps)" in src, "대조군 시뮬레이터에 안 넘긴다"


def test_cutoff_is_cross_sectional_not_a_fixed_amount():
    """전 기간 고정 금액이면 지수가 오르면 저절로 느슨해진다 — 시장 수준 규칙이 된다."""
    src = inspect.getsource(hz._run_phase)
    assert "min_cap_pct / 100.0" in src and "for t in avail" in src.split("cap_cut")[1][:400], \
        "컷오프를 그 날의 후보 집합에서 재지 않는다"


def test_unknown_marketcap_does_not_block():
    """모르면 막지 않는다 — 전 종목 차단은 신중함이 아니라 0으로 나누기다."""
    panel = _panel()
    order = sorted(panel.closes)
    caps = {t: [None] * len(panel.dates) for t in order}
    out = hz.run(panel, _cfg(min_mktcap_pct=50.0), scores=_scores(panel, order),
                 caps=caps, score_source="external")
    assert out["mktcap_gate"]["blocked"] == 0
    assert out["strategy"]["avg_picks"] > 0, "시총을 모른다고 전 종목을 막았다"


def test_it_is_reachable_from_the_route():
    """검사에 넣을 수 없는 파라미터는 검증된 적이 없다 — 실험 파라미터는 API로 들어와야 한다."""
    import pathlib
    api_src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "api.py"
               ).read_text(encoding="utf-8")
    assert "min_mktcap_pct=float(ov.get(\"min_mktcap_pct\")" in api_src
    assert "min_mktcap_pct" in inspect.signature(__import__(
        "signal_desk.store", fromlist=["store"]).run_harness).parameters
