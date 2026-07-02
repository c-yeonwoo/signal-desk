from signal_desk.signals import risk


def test_no_exit_when_within_bounds():
    assert risk.check_exit(avg_price=100, last_close=98, peak=100) is None


def test_stop_loss_at_exact_threshold():
    assert risk.check_exit(avg_price=100, last_close=93, peak=100) == "STOP_LOSS"


def test_stop_loss_beyond_threshold():
    assert risk.check_exit(avg_price=100, last_close=90, peak=100) == "STOP_LOSS"


def test_take_profit_at_exact_threshold():
    assert risk.check_exit(avg_price=100, last_close=115, peak=115) == "TAKE_PROFIT"


def test_take_profit_beyond_threshold():
    assert risk.check_exit(avg_price=100, last_close=120, peak=120) == "TAKE_PROFIT"


def test_trailing_triggers_on_drawdown_from_peak():
    # pl=+10%(익절 미달), peak 대비 -8.3% 하락 -> 트레일링
    assert risk.check_exit(avg_price=100, last_close=110, peak=120) == "TRAILING"


def test_stop_loss_takes_priority_over_trailing():
    # 손절 조건도 만족하고 고점 대비 하락도 크지만, brightdesk 순서대로 손절이 먼저 판정됨
    assert risk.check_exit(avg_price=100, last_close=90, peak=200) == "STOP_LOSS"


def test_peak_since_entry():
    assert risk.peak_since_entry([100, 105, 102, 110, 108], entry_idx=1) == 110
    assert risk.peak_since_entry([100, 105, 102, 110, 108], entry_idx=0) == 110


def test_check_exit_from_series_trailing():
    result = risk.check_exit_from_series([100, 105, 120, 110], entry_idx=0, avg_price=100)
    assert result == "TRAILING"


def test_custom_config_overrides_default():
    tight = risk.RiskConfig(stop_loss_pct=-0.03)
    assert risk.check_exit(avg_price=100, last_close=96, peak=96, config=tight) == "STOP_LOSS"
    assert risk.check_exit(avg_price=100, last_close=96, peak=96) is None  # 기본값(-7%)으론 미충족
