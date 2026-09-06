"""하네스 청산 레이어 — 라이브가 실제로 쓰는 청산 규칙을 검사에 넣는다.

2026-09-06 진단: 레퍼런스 3봇의 최근 20거래에서 **매도 사유 100%가 `TRAILING`** 이었는데,
`harness.py` 에는 `stop_loss`·`trailing`·`take_profit` 이라는 단어가 한 번도 없었다.
즉 판별력 판정은 '5일 무청산 보유'를 재고 있었고, 실제로 돈을 잃은 규칙은 **검사에 들어간
적이 없다.** 이 파일이 그 구멍을 막는다.
"""

from __future__ import annotations

import dataclasses

from signal_desk.signals import harness, risk


def _panel(rows: dict[str, list[float]]) -> harness.Panel:
    n = len(next(iter(rows.values())))
    dates = [f"2026-01-{i + 1:02d}" for i in range(n)]
    return harness.Panel(dates=dates, closes={t: list(v) for t, v in rows.items()})


def test_no_rules_holds_to_period_end():
    """규칙이 없으면 기간 끝까지 보유 — 2026-09-06 이전의 유일한 동작이 보존된다."""
    # 100 → 80(중간 급락) → 120(회복). 무청산이면 +20%.
    p = _panel({"A": [100, 100, 80, 90, 100, 110, 120]})
    cfg = harness.HarnessConfig(rebalance_days=5, override_selection=True)
    ret, exited = harness._period_return(p, ["A"], 0, cfg)
    assert round(ret, 4) == 0.20
    assert exited == set()


def test_stop_loss_exits_inside_the_period():
    """같은 경로에 손절을 걸면 −20%에서 나간다 — 규칙이 결과를 바꾼다."""
    p = _panel({"A": [100, 100, 80, 90, 100, 110, 120]})
    cfg = harness.HarnessConfig(
        rebalance_days=5, override_selection=True,
        exit_rules=risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                                   trailing_from_peak_pct=-0.99))
    ret, exited = harness._period_return(p, ["A"], 0, cfg)
    assert round(ret, 4) == -0.20      # 80/100 - 1
    assert exited == {"A"}


def test_trailing_measures_from_peak_not_entry():
    p = _panel({"A": [100, 100, 130, 110, 115, 120, 125]})
    cfg = harness.HarnessConfig(
        rebalance_days=5, override_selection=True,
        exit_rules=risk.RiskConfig(stop_loss_pct=-0.99, take_profit_pct=0.99,
                                   trailing_from_peak_pct=-0.10))
    ret, exited = harness._period_return(p, ["A"], 0, cfg)
    # 고점 130 → 110 은 -15.4% 되돌림이라 트레일링 발동. 진입가 대비로는 +10%.
    assert round(ret, 4) == 0.10
    assert exited == {"A"}


def test_harness_uses_the_live_exit_function(monkeypatch):
    """청산 판정은 **라이브와 같은 함수**로 한다.

    하네스가 따로 조립하면 그 차이가 판별력으로 둔갑한다. `risk.check_exit` 를 막으면
    하네스의 조기청산도 사라져야 한다 — 안 사라지면 어딘가에 사본이 있는 것이다.
    """
    monkeypatch.setattr(risk, "check_exit", lambda *a, **k: None)
    p = _panel({"A": [100, 100, 50, 50, 50, 50, 50]})
    cfg = harness.HarnessConfig(
        rebalance_days=5, override_selection=True,
        exit_rules=risk.RiskConfig(stop_loss_pct=-0.01, take_profit_pct=0.01,
                                   trailing_from_peak_pct=-0.01))
    _ret, exited = harness._period_return(p, ["A"], 0, cfg)
    assert exited == set(), "risk.check_exit 를 막았는데도 청산됐다 — 하네스에 사본이 있다"


def test_early_exit_costs_turnover_next_period():
    """조기청산된 종목은 다음 기간에 '보유'로 세지 않는다.

    세면 재매수 비용이 사라져 청산 규칙이 공짜로 보인다. 회전율이 다른 두 경로를 비교하면
    회전율 차이가 판별력으로 둔갑한다 — 이 리포가 로테이션 대조군에서 이미 겪은 병이다.
    """
    src = open(harness.__file__, encoding="utf-8").read()
    assert "held = set(picks) - exited" in src


def test_run_declares_the_exit_layer():
    """산출물이 청산 레이어를 **선언**한다 — 선언이 없으면 읽는 사람이 같은 전략이라 가정한다."""
    rows = {f"T{i}": [100 + i + j * (1 + i % 3) for j in range(400)] for i in range(12)}
    p = _panel(rows)
    out = harness.run(p, harness.HarnessConfig(
        warmup=130, rebalance_days=5, random_trials=2, min_periods=1,
        override_selection=True, min_score=-99, top_pct=50.0))
    assert "exit_layer" in out
    assert out["exit_layer"]["rules"] is None
    assert "무청산" in out["exit_layer"]["note"]
    assert out["exit_layer"]["intraday"] is False


def test_run_counts_how_often_the_rules_actually_fired():
    """규칙을 넣었는데 발동이 0이면 그 규칙은 검증된 적이 없는 것과 같다."""
    # 톱니 경로 — 오르내림이 커서 트레일링이 반드시 걸린다.
    rows = {f"T{i}": [100 + (j % 7) * (3 + i) + j * 0.2 for j in range(400)] for i in range(12)}
    p = _panel(rows)
    cfg = harness.HarnessConfig(
        warmup=130, rebalance_days=5, random_trials=2, min_periods=1,
        override_selection=True, min_score=-99, top_pct=50.0,
        exit_rules=risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.09,
                                   trailing_from_peak_pct=-0.05))
    out = harness.run(p, cfg)
    assert out["exit_layer"]["rules"]["trailing_from_peak_pct"] == -0.05
    assert out["exit_layer"]["n_positions"] > 0
    assert out["exit_layer"]["early_exits"] > 0, "청산 규칙이 한 번도 발동하지 않았다"
    assert out["exit_layer"]["early_exit_pct"] > 0


def test_benchmark_never_gets_exit_rules():
    """벤치마크는 정의상 동일가중 **매수보유**다 — 청산을 걸면 대조군이 아니라 다른 전략이다."""
    src = open(harness.__file__, encoding="utf-8").read()
    assert "bench_cfg" in src
    assert "dataclasses.replace(cfg, exit_rules=None)" in src


def test_exit_rules_default_off_so_existing_verdicts_do_not_move():
    """기본값은 무청산. 사전등록된 판정의 대상이 조용히 바뀌면 안 된다."""
    assert harness.HarnessConfig().exit_rules is None
    assert dataclasses.fields(harness.HarnessConfig)  # 필드가 실재한다
