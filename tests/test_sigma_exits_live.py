"""σ 스케일 청산의 **라이브 배선** — 하네스에만 있으면 봇은 그대로다.

이 리포가 네 번 밟은 병: 코드는 있는데 아무도 안 부른다(시세 수집·투자경고 veto·PIT 컬럼·
고아 라우트). 청산 폭에서 다시 밟지 않도록 배선 자체를 검사한다.
"""

from __future__ import annotations

from signal_desk import bot, config, strategy
from signal_desk.signals import harness, vol_sizing


def test_bot_computes_sigma_per_holding():
    src = open(bot.__file__, encoding="utf-8").read()
    assert "def _risk_for(" in src, "종목별 청산 폭을 만드는 곳이 없다"
    assert "pos_risk = _risk_for(closes)" in src, "보유 루프에서 종목별 폭을 안 쓴다"
    assert "risk.check_exit(avg_price, current_price, peak, pos_risk)" in src, \
        "청산 판정이 여전히 공용 고정폭을 쓴다"


def test_sell_note_reports_the_effective_width():
    """문구가 명목 폭을 말하면 '왜 여기서 잘렸나'가 안 맞는다."""
    src = open(bot.__file__, encoding="utf-8").read()
    assert "pos_risk.effective()" in src, "매도 사유 문구가 환산 전 폭을 쓴다"


def test_kill_switch_exists_and_defaults_on(monkeypatch):
    monkeypatch.delenv("SIGMA_SCALED_EXITS", raising=False)
    assert config.sigma_scaled_exits() is True
    for off in ("0", "false", "no", "NO"):
        monkeypatch.setenv("SIGMA_SCALED_EXITS", off)
        assert config.sigma_scaled_exits() is False, f"{off}로 못 끈다"
    monkeypatch.setenv("SIGMA_SCALED_EXITS", "1")
    assert config.sigma_scaled_exits() is True


def test_sigma_definition_is_shared_with_the_harness():
    """하네스와 라이브가 다른 σ를 쓰면 검사가 라이브를 재지 않는다.

    같은 깨끗한 시계열에서 두 구현이 같은 값을 내야 한다.
    """
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.0 + (0.02 if i % 3 else -0.017)))
    live = vol_sizing.realized_vol(closes)
    hz = harness._realized_sigma(closes, len(closes) - 1)
    assert live is not None and hz is not None
    assert abs(live - hz) < 1e-9, f"라이브 {live} vs 하네스 {hz} — 정의가 갈라졌다"


def test_widths_actually_widen_for_a_volatile_name():
    quiet = strategy.risk_config("balanced", "약세", sigma=0.015).effective()
    wild = strategy.risk_config("balanced", "약세", sigma=0.06).effective()
    assert wild.stop_loss_pct < quiet.stop_loss_pct
    assert wild.trailing_from_peak_pct < quiet.trailing_from_peak_pct


def test_registration_declares_the_new_family():
    from signal_desk import prereg
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    lk = next((x for x in reg["looks"] if x["id"] == "sigma-exits-oos"), None)
    assert lk is not None, "σ 청산 family가 등록에 없다"
    assert lk["harness"]["exits"] == "balanced+sigma"
    assert lk["requirement"]["from_date"] >= lk["registered_at"], \
        "결과를 본 가설은 아직 보지 않은 구간에만 걸어야 한다"


def test_adding_the_look_raised_the_threshold_for_everyone():
    """n을 늘린 대가가 실제로 치러졌는지 — 안 오르면 사후 완화다."""
    from signal_desk import prereg
    reg = prereg.load()
    assert reg["n_looks_total"] == 6
    assert reg["threshold_pct"] == prereg.sidak_threshold_pct(6)
    assert reg["threshold_pct"] > prereg.sidak_threshold_pct(5)
