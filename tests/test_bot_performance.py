"""봇 track record — 일별 자산 스냅샷 기록 + 성과(수익률·MDD·자산곡선) 집계."""

import json

from signal_desk import bot, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalResult

UID = 21


def _setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    uni = [{"ticker": "AAA", "name": "가"}]
    monkeypatch.setattr(bot.store, "load_universe", lambda: uni)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {"AAA": [100.0, 110.0]})
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: [
        SignalResult(ticker="AAA", name="가", score=2.0, kind="BUY", confidence=0.6,
                     technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])])
    monkeypatch.setattr(bot, "_market_read", lambda p: {"eff_cfg": None, "adapt": {}, "context": {"regime": "중립"}})
    monkeypatch.setattr(bot, "_cfg", lambda uid: {
        "enabled": True, "trading_style": "balanced", "seed_cash": 1_000_000, "seed_cash_us": 10_000,
        "max_positions": 10, "position_pct": 0.1, "min_buy_score": 1.0, "max_new_buys_per_run": 5})


def test_run_records_daily_equity(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    bot.run_once(UID, market="kr")
    curve = db.bot_equity_curve(UID, "kr")
    assert len(curve) == 1 and curve[0]["total_eval"] > 0    # 오늘 자산 1점 기록됨


def test_performance_summary(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    # 자산곡선 수동 시딩(수익 후 낙폭)
    for d, te in [("2026-07-01", 1_000_000), ("2026-07-02", 1_100_000), ("2026-07-03", 1_045_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)
    perf = bot.performance(UID, "kr")
    assert perf["seed"] == 1_000_000 and perf["days"] == 3
    assert perf["max_drawdown_pct"] == -5.0                   # 110만 → 104.5만 = -5%
    assert perf["return_pct"] is not None and perf["currency"] == "KRW"


def test_since_seed_return_is_not_subtracted_from_shorter_benchmark_window(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    db.bot_equity_record(UID, "kr", "2026-09-22", 900_000, 900_000, 0)
    db.bot_equity_record(UID, "kr", "2026-09-23", 990_000, 990_000, 0)
    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", lambda *a, **k: ([
        {"date": "2026-09-22", "total_eval": 1.0},
        {"date": "2026-09-23", "total_eval": 1.05}], None))
    out = bot.performance(UID)
    assert out["return_pct"] == 0.0              # 현재 계좌 / 시드
    assert out["comparison_return_pct"] == 10.0   # 기록된 첫날 / 마지막날
    assert out["benchmark_return_pct"] == 5.0
    assert out["excess_return_pct"] == 5.0         # 10−5, 0−5가 아님
    assert out["max_drawdown_pct"] == -10.0        # 시드 기준 첫 기록의 하락도 포함


def test_non_session_marks_leave_the_comparison_and_stay_named(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    for d, te in [("2026-08-14", 1_000_000), ("2026-08-17", 1_500_000),
                  ("2026-08-18", 1_100_000), ("2026-08-22", 2_000_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)
    seen = {}

    def fake(curve, market="kr", **_k):
        seen["dates"] = [p["date"] for p in curve]
        built = [{"date": day, "total_eval": 1.0} for day in seen["dates"]]
        built[-1]["total_eval"] = 1.02
        return built, None

    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", fake)
    out = bot.performance(UID, "kr")
    assert seen["dates"] == ["2026-08-14", "2026-08-18"]
    assert out["comparison_return_pct"] == 10.0   # 110/100. 토요일 200만이면 100%다.
    assert out["excess_return_pct"] == 8.0
    assert out["excluded_non_sessions"] == ["2026-08-17", "2026-08-22"]
    assert "2026-08-17" in out["benchmark_basis"] and "비교에서 제외" in out["benchmark_basis"]


def test_us_holiday_mark_does_not_truncate_the_session_window(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 10_000.0, "positions": {}}))
    for d, te in [("2026-07-02", 10_000), ("2026-07-03", 10_500), ("2026-07-06", 11_000)]:
        db.bot_equity_record(UID, "us", d, te, te, 0)
    seen = {}

    def fake(curve, market="us", **_k):
        seen["dates"] = [p["date"] for p in curve]
        return [{"date": day, "total_eval": 1.0} for day in seen["dates"]], None

    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", fake)
    history = {"2026-07-01": [{"ticker": "AAA"}], "2026-07-02": [{"ticker": "AAA"}]}
    out = bot.performance(UID, "us", universe_history=history)
    assert seen["dates"] == ["2026-07-02", "2026-07-06"]
    assert out["excluded_non_sessions"] == ["2026-07-03"]
    assert out["comparison_return_pct"] == 10.0


def test_a_missing_price_drops_only_the_broken_prefix(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    for d, te in [("2026-09-22", 1_000_000), ("2026-09-23", 1_100_000), ("2026-09-28", 1_210_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)
    calls = []

    def fake(curve, market="kr", **_k):
        dates = [p["date"] for p in curve]
        calls.append(dates)
        if dates and dates[0] == "2026-09-22":
            return None, "비교 불가 — 2026-09-22→2026-09-23 가격 결측 1종목 (017960)"
        built = [{"date": day, "total_eval": 1.0} for day in dates]
        built[-1]["total_eval"] = 1.05
        return built, None

    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", fake)
    out = bot.performance(UID, "kr")
    assert calls == [["2026-09-22", "2026-09-23", "2026-09-28"], ["2026-09-23", "2026-09-28"]]
    assert out["comparison_return_pct"] == 10.0
    assert out["excess_return_pct"] == 5.0
    assert out["price_gap_notes"] == ["비교 불가 — 2026-09-22→2026-09-23 가격 결측 1종목 (017960)"]


def test_a_missing_session_is_not_trimmed_into_a_comparison(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    for d, te in [("2026-09-22", 1_000_000), ("2026-09-28", 1_100_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)
    calls = []

    def fake(curve, market="kr", **_k):
        calls.append([p["date"] for p in curve])
        return None, "비교 불가 — 2026-09-22→2026-09-28 연속 거래세션 아님"

    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", fake)
    out = bot.performance(UID, "kr")
    assert calls == [["2026-09-22", "2026-09-28"]]
    assert out["excess_return_pct"] is None and out["price_gap_notes"] == []
    assert "연속 거래세션 아님" in out["benchmark_basis"]


def test_price_holes_through_the_curve_stay_one_sentence(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    for d, te in [("2026-09-22", 1_000_000), ("2026-09-23", 1_100_000), ("2026-09-28", 1_210_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)

    def fake(curve, market="kr", **_k):
        dates = [p["date"] for p in curve]
        if len(dates) < 2:
            return None, "비교 불가 — 평가일 2개 미만"
        return None, f"비교 불가 — {dates[0]}→{dates[1]} 가격 결측 1종목 (017960)"

    monkeypatch.setattr(bot.performance_evidence, "pit_equal_weight_detail", fake)
    out = bot.performance(UID, "kr")
    assert out["excess_return_pct"] is None and out["price_gap_notes"] == []
    assert out["benchmark_basis"].count("017960") == 1
    assert "2026-09-23→2026-09-28" not in out["benchmark_basis"]
    assert "가격이 빈 세션 쌍 2개라 비교할 구간이 없다" in out["benchmark_basis"]
