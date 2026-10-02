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


def test_biggest_riser_outside_the_buy_window_is_named_and_not_an_order():
    """+10% 매수보다 +18% 관망이 문장의 주어다. kind는 그대로다."""
    buy = _sig("267250", kind="BUY", rank=2, score=1.99)
    riser = _sig("020150", kind="HOLD", rank=177, eligible=False, score=-0.48)
    riser.hold_tag = "데이터부족"
    quiet = _sig("000660", kind="HOLD", rank=10, eligible=False, score=1.47)
    out = desk_report.build(
        [buy, riser, quiet],
        selection={"mode": "rank", "rank_slots": 6, "eligible": 1},
        move_rows=[
            {"ticker": "267250", "change_pct": 10.0},
            {"ticker": "020150", "change_pct": 18.6},
            {"ticker": "000660", "change_pct": 0.2},
        ])
    assert out["outside_movers"][0]["ticker"] == "020150"
    assert out["outside_note"] == (
        "오늘 최대 상승 020150 +18.6% · 177위 · 점수 -0.48 · 데이터부족 · 매수 아님")
    assert buy.kind == "BUY"
    assert "267250" not in out["outside_note"]


def test_today_card_prints_the_server_sentence():
    html = open("src/signal_desk/web/index.html", encoding="utf-8").read()
    assert "rep.outside_note" in html
    assert "이 문장은 주문을 바꾸지 않는다" in html


def test_crowding_real_sector_still_warns():
    buys = [_sig("005930"), _sig("000660"), _sig("042700")]
    out = crowding.assess(buys)
    assert out["warn"] is True
    assert out["data_quality"] is False
    assert out["top_sector"] == "반도체"
