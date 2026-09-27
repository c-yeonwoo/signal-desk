"""성과는 같은 세션·당시 유니버스·원장 평가액으로만 비교한다."""

from signal_desk import bot
from signal_desk.signals import performance_evidence as evidence


def test_pit_benchmark_uses_past_universe_and_compounds(monkeypatch):
    days = ["2026-09-22", "2026-09-23", "2026-09-28"]  # 추석은 건너뛴 연속 거래세션
    monkeypatch.setattr(evidence.store, "universe_at", lambda day: [
        {"ticker": "A"}, {"ticker": "B"}] if day == "2026-09-21" else [{"ticker": "A"}])
    monkeypatch.setattr(evidence.store, "load_all_dated_closes", lambda: {
        "A": (days, [100.0, 110.0, 121.0]),
        "B": (days, [100.0, 90.0, 1.0]),
        "NEW": (days, [100.0, 1000.0, 2000.0]),
    })
    curve = [{"date": day, "total_eval": 100.0} for day in days]
    bench = evidence.pit_equal_weight_curve(curve)
    assert bench is not None
    assert bench[-1]["total_eval"] == 1.1  # 첫 구간 (1.1+0.9)/2, 다음 구간 A만 +10%


def test_pit_benchmark_refuses_missing_delisted_price(monkeypatch):
    days = ["2026-09-22", "2026-09-23"]
    monkeypatch.setattr(evidence.store, "universe_at", lambda _: [{"ticker": "A"}, {"ticker": "DELISTED"}])
    monkeypatch.setattr(evidence.store, "load_all_dated_closes", lambda: {
        "A": (days, [100.0, 110.0]), "DELISTED": ([days[0]], [100.0])})
    assert evidence.pit_equal_weight_curve([{"date": d} for d in days]) is None


def test_pit_benchmark_refuses_missing_account_session(monkeypatch):
    called = []
    monkeypatch.setattr(evidence.store, "load_all_dated_closes", lambda: called.append(True) or {})
    assert evidence.pit_equal_weight_curve([{"date": "2026-09-22"}, {"date": "2026-09-28"}]) is None
    assert called == []  # 세션 불연속이면 가격을 읽기도 전에 기권


def test_paired_harm_refuses_mismatched_dates():
    curve = [{"date": str(i), "total_eval": 100.0} for i in range(45)]
    bench = [{"date": str(i), "total_eval": 1.0} for i in range(44)] + [
        {"date": "wrong", "total_eval": 1.0}]
    out = bot.harm_alert(curve, seed=100, benchmark_curve=bench)
    assert not out["ready"] and not out["alert"] and "불일치" in out["reason"]
