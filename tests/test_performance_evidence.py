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
    curve = [{"date": d} for d in days]
    assert evidence.pit_equal_weight_curve(curve) is None
    _built, reason = evidence.pit_equal_weight_detail(curve)
    assert reason == "비교 불가 — 2026-09-22→2026-09-23 가격 결측 1종목 (DELISTED)"


def test_pit_benchmark_refuses_missing_account_session(monkeypatch):
    called = []
    monkeypatch.setattr(evidence.store, "load_all_dated_closes", lambda: called.append(True) or {})
    curve = [{"date": "2026-09-22"}, {"date": "2026-09-28"}]
    assert evidence.pit_equal_weight_curve(curve) is None
    _built, reason = evidence.pit_equal_weight_detail(curve)
    assert "2026-09-22→2026-09-28" in reason and "연속 거래세션 아님" in reason
    assert called == []  # 세션 불연속이면 가격을 읽기도 전에 기권


def test_session_points_drop_a_holiday_and_detail_still_refuses_it(monkeypatch):
    curve = [
        {"date": "2026-08-14", "total_eval": 100.0},
        {"date": "2026-08-17", "total_eval": 150.0},  # 광복절 대체휴무
        {"date": "2026-08-18", "total_eval": 110.0},
        {"date": "2026-08-22", "total_eval": 200.0},  # 토요일
    ]
    kept, dropped = evidence.session_points(curve, "kr")
    assert [p["date"] for p in kept] == ["2026-08-14", "2026-08-18"]
    assert dropped == ["2026-08-17", "2026-08-22"]
    note = evidence.non_session_note(dropped)
    assert "2026-08-17" in note and "2026-08-22" in note and "비교에서 제외" in note
    called = []
    monkeypatch.setattr(evidence.store, "load_all_dated_closes", lambda: called.append(True) or {})
    _built, reason = evidence.pit_equal_weight_detail(curve)
    assert "2026-08-14→2026-08-17" in reason and "연속 거래세션 아님" in reason
    assert called == []  # 휴장일 평가는 여기서 지우지 않는다. 표본 선택은 호출자 몫이다.


def test_paired_harm_refuses_mismatched_dates():
    curve = [{"date": str(i), "total_eval": 100.0} for i in range(45)]
    bench = [{"date": str(i), "total_eval": 1.0} for i in range(44)] + [
        {"date": "wrong", "total_eval": 1.0}]
    out = bot.harm_alert(curve, seed=100, benchmark_curve=bench)
    assert not out["ready"] and not out["alert"] and "불일치" in out["reason"]
