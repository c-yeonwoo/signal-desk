"""장중 표본 주기 — 하네스는 하루 1표본인데 라이브 매도는 5분틱(약 78표본)이다.

경로 의존 규칙(트레일링·손절)은 표본이 많을수록 더 자주 걸린다. 실측(브리지):

    표본/일   조기청산율   평균 보유
       1        79.7%     10.4일   ← 하네스
      13        87.9%      7.9일   ← 30분틱(매수)
      78        89.5%      7.4일   ← 5분틱(매도)

즉 하네스는 조기청산을 9.8%p 적게, 보유기간을 3일 길게 잡는다. 30분↔5분은 1.6%p뿐이라
**매수/매도 주기를 맞추는 것은 실익이 없다** — 격차의 본체는 1표본 vs 다표본이다.
"""

from __future__ import annotations

import statistics

from signal_desk.signals import harness as hz, risk


def test_default_is_close_only():
    assert hz.HarnessConfig().intraday_samples == 1


def test_single_sample_returns_the_close():
    assert hz._intraday_path(100.0, 105.0, 1, 0.035, 7) == [105.0]


def test_path_starts_from_the_diffusion_and_ends_exactly_at_close():
    p = hz._intraday_path(100.0, 103.0, 20, 0.035, 7)
    assert len(p) == 20
    assert p[-1] == 103.0, "마지막 표본이 종가가 아니면 일간 수익이 달라진다"


def test_path_is_deterministic_per_ticker_and_day():
    """대조군과 같은 경로를 써야 한다 — 시드가 흔들리면 기계적 조건이 갈라진다."""
    a = hz._intraday_path(100.0, 103.0, 30, 0.035, 12345)
    b = hz._intraday_path(100.0, 103.0, 30, 0.035, 12345)
    assert a == b
    assert a != hz._intraday_path(100.0, 103.0, 30, 0.035, 12346)


def test_ticker_seed_is_stable_across_processes():
    """`hash()` 는 실행마다 소금이 달라 재현이 깨진다."""
    import zlib
    assert hz._ticker_seed("005930") == zlib.crc32(b"005930")


def test_more_samples_raises_early_exits_in_expectation():
    """표본이 많아지면 경로 의존 규칙은 **더 자주** 걸린다.

    단조성은 **기댓값**에서만 성립한다 — 브리지는 k가 다르면 난수 개수가 달라 다른 경로를
    만든다(중첩 정제가 아니다). 그래서 여러 티커·진입일로 평균을 낸다. 이 사실 자체가
    실측을 읽을 때 중요하다: 한 종목 한 케이스를 비교하면 방향이 뒤집힐 수 있다.
    """
    n = 60
    rules = risk.RiskConfig(stop_loss_pct=-0.15, take_profit_pct=0.50,
                            trailing_from_peak_pct=-0.06)
    rates = {}
    for k in (1, 13, 78):
        early = tot = 0
        for tk in range(40):
            row = [100.0 * (1.0 + 0.015 * ((-1) ** j) + 0.001 * tk * j) for j in range(n)]
            for e in range(1, 35, 3):
                _r, was = hz._exit_walk(row, e, min(e + 12, n - 1), rules,
                                        samples=k, sigma=0.035,
                                        seed_base=hz._ticker_seed(f"T{tk}"))
                early += bool(was)
                tot += 1
        rates[k] = early / tot
    assert rates[13] > rates[1], rates
    assert rates[78] >= rates[13] - 0.02, rates      # 13→78은 실측에서도 1.6%p뿐이다


def test_measured_sigma_is_recorded_with_provenance():
    src = open(hz.__file__, encoding="utf-8").read()
    assert "0.0352" in src
    assert "46,406 종목-일" in src, "실측 σ의 출처가 없으면 다음 사람이 다시 추측한다"


def test_run_declares_it_as_a_model_not_data():
    rows = {f"T{i}": [100.0 + (j % 5) * (2 + i) + j * 0.1 for j in range(400)]
            for i in range(12)}
    p = hz.Panel(dates=[f"2026-01-{i:03d}" for i in range(400)], closes=rows)
    cfg = hz.HarnessConfig(warmup=130, rebalance_days=5, random_trials=2, min_periods=1,
                           override_selection=True, min_score=-99, top_pct=50.0,
                           intraday_samples=13,
                           exit_rules=risk.RiskConfig(stop_loss_pct=-0.07,
                                                      take_profit_pct=0.09,
                                                      trailing_from_peak_pct=-0.05))
    out = hz.run(p, cfg)
    el = out["exit_layer"]
    assert el["intraday_samples"] == 13
    assert el["intraday_sigma"] == cfg.intraday_sigma
    assert "모델" in el["note"], "모델을 데이터처럼 읽히게 두면 안 된다"


def test_close_only_note_carries_the_measured_gap():
    """정성 문구만 두면 다음 사람이 크기를 모른다."""
    rows = {f"T{i}": [100.0 + j * 0.2 for j in range(400)] for i in range(12)}
    p = hz.Panel(dates=[f"2026-01-{i:03d}" for i in range(400)], closes=rows)
    out = hz.run(p, hz.HarnessConfig(
        warmup=130, rebalance_days=5, random_trials=2, min_periods=1,
        override_selection=True, min_score=-99, top_pct=50.0,
        exit_rules=risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.09,
                                   trailing_from_peak_pct=-0.05)))
    assert out["exit_layer"]["intraday_samples"] == 1
    assert "9.8%p" in out["exit_layer"]["note"]


def test_registration_pins_the_sampling_rate():
    """등록이 표본 주기를 못 박지 않으면 같은 id가 두 전략을 잰다(79.7% vs 89.5%)."""
    from signal_desk import prereg
    assert "intraday_samples" in prereg._HARNESS_KEYS
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    for lk in reg["looks"]:
        assert lk["harness"]["intraday_samples"] == 1, f"{lk['id']}: 표본 주기 선언이 없다"


def test_declaring_it_did_not_move_the_threshold():
    from signal_desk import prereg
    reg = prereg.load()
    assert reg["n_looks_total"] == 6
    assert reg["threshold_pct"] == prereg.sidak_threshold_pct(6)


def test_run_preregistered_and_cli_both_read_it():
    from signal_desk import store
    src = open(store.__file__, encoding="utf-8").read()
    assert 'hzc.get("intraday_samples")' in src
    cli = open("src/signal_desk/cli.py", encoding="utf-8").read() \
        if __import__("os").path.exists("src/signal_desk/cli.py") else ""
    assert "--intraday-samples" in cli, "CLI로 못 켜면 아무도 재지 않는다"
