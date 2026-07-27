"""매수 에피소드 · 진입 품질 — kind와 분리된 추격도."""

from signal_desk.signals import entry_quality as eq


def test_episode_starts_after_hold_break():
    hist = [
        ("2026-07-20", "BUY"),
        ("2026-07-21", "HOLD"),
        ("2026-07-22", "BUY"),
        ("2026-07-23", "STRONG_BUY"),
    ]
    dates = [d for d, _ in hist] + ["2026-07-24"]
    closes = [1000.0, 1050.0, 1100.0, 1200.0, 1300.0]
    out = eq.compute(
        "T", kind="BUY", price=1300.0, hist_days=hist,
        dates=dates, closes=closes, today="2026-07-24",
    )
    assert out is not None
    assert out["fire_date"] == "2026-07-22"
    assert out["fire_price"] == 1100.0
    assert out["run_up_pct"] == 18.2  # 1300/1100 - 1
    assert out["age_days"] == 2  # 22,23,24 → age 2


def test_fresh_on_first_day():
    out = eq.compute(
        "T", kind="STRONG_BUY", price=100.0, hist_days=[],
        dates=["2026-07-24"], closes=[100.0], today="2026-07-24",
    )
    assert out["quality"] == "fresh"
    assert out["age_days"] == 0
    assert out["run_up_pct"] == 0.0
    assert out["quality_ko"] == "신선"


def test_late_when_run_up_high():
    hist = [("2026-07-01", "BUY"), ("2026-07-02", "BUY"), ("2026-07-03", "BUY")]
    dates = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-10"]
    closes = [100.0, 105.0, 110.0, 140.0]
    out = eq.compute(
        "T", kind="BUY", price=140.0, hist_days=hist,
        dates=dates, closes=closes, today="2026-07-10",
    )
    assert out["fire_date"] == "2026-07-01"
    assert out["run_up_pct"] == 40.0
    assert out["quality"] == "late"


def test_late_when_remain_upside_thin():
    assert eq.classify_quality(3.0, 0, remain_upside_pct=4.0) == "late"
    assert eq.classify_quality(3.0, 0, remain_upside_pct=10.0) == "fresh"


def test_hold_has_no_entry():
    assert eq.compute(
        "T", kind="HOLD", price=100.0, hist_days=[("2026-07-24", "HOLD")],
        dates=["2026-07-24"], closes=[100.0], today="2026-07-24",
    ) is None


def test_annotate_rows_sets_entry_only_on_buys():
    rows = [
        {"ticker": "A", "kind": "BUY", "price": 110.0},
        {"ticker": "B", "kind": "HOLD", "price": 90.0},
    ]
    hist = {"A": [("2026-07-23", "BUY")], "B": []}
    dates = {"A": ["2026-07-23", "2026-07-24"], "B": ["2026-07-24"]}
    closes = {"A": [100.0, 110.0], "B": [90.0]}
    eq.annotate_rows(rows, hist_by=hist, dates_by=dates, closes_by=closes, today="2026-07-24")
    assert rows[0]["entry"]["fire_price"] == 100.0
    assert rows[0]["entry"]["run_up_pct"] == 10.0
    assert rows[1]["entry"] is None
