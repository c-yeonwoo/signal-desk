"""Desk Report(L4) · crowding 미분류=데이터공백."""

from signal_desk.signals import crowding, desk_report
from signal_desk.signals.engine import SignalResult


def _sig(ticker, *, kind="BUY", score=1.5, rank=1, eligible=True, reasons=None,
         gate=False, event=False):
    r = SignalResult(
        ticker=ticker, name=ticker, score=score, kind=kind, confidence=0.5,
        technical_score=0, fundamental_score=0, has_fundamental=False,
        gate_blocked=gate, event_risk=event,
        reasons=list(reasons or []),
    )
    r.rank = rank
    r.rank_eligible = eligible
    return r


def test_desk_report_wait_stance_and_vacancies():
    # 창 3자리 · 1매수 · 2공석(게이트)
    rows = [
        _sig("005930", kind="BUY", rank=1, eligible=True, score=1.8),
        _sig("000660", kind="HOLD", rank=2, eligible=False, score=1.5, gate=True,
             reasons=["[선정] 시장 10종목 중 2위 — 게이트·악재로 자리 공석"]),
        _sig("035420", kind="HOLD", rank=3, eligible=False, score=0.9,
             reasons=["[선정] 시장 10종목 중 3위 — 최소점수 1.2 미달로 자리 공석"]),
        _sig("005380", kind="HOLD", rank=4, eligible=False, score=0.5),
    ]
    sel = {"mode": "rank", "universe": 10, "rank_slots": 3, "eligible": 1,
           "rank_min_score": 1.2, "cutoff_score": 1.8}
    out = desk_report.build(rows, selection=sel, exposure=0.7)
    assert out["ready"] is True
    assert out["stance"] == "partial"
    assert len(out["buys"]) == 1
    assert len(out["vacancies"]) == 2
    assert out["vacancies"][0]["ticker"] == "000660"
    assert "공석" in out["vacancies"][0]["note"]


def test_desk_report_zero_buy_is_wait_not_broken():
    rows = [_sig("005930", kind="HOLD", rank=1, eligible=False, score=0.8,
                 reasons=["[선정] — 최소점수 1.2 미달로 자리 공석"])]
    out = desk_report.build(rows, selection={
        "mode": "rank", "universe": 200, "rank_slots": 6, "eligible": 0})
    assert out["stance"] == "wait"
    assert "정밀도" in out["headline"]


def test_crowding_unmapped_is_data_quality_not_warn():
    # 섹터맵에 없는 가짜 티커 3개
    buys = [_sig("ZZZZ01"), _sig("ZZZZ02"), _sig("ZZZZ03")]
    out = crowding.assess(buys)
    assert out["top_sector"] == "미분류"
    assert out["data_quality"] is True
    assert out["warn"] is False
    assert "편중 아님" in out["note"]


def test_crowding_real_sector_still_warns():
    buys = [_sig("005930"), _sig("000660"), _sig("042700")]
    out = crowding.assess(buys)
    assert out["warn"] is True
    assert out["data_quality"] is False
    assert out["top_sector"] == "반도체"
