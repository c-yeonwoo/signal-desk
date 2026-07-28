"""실행 게이트 — late·선반영이면 BUY→HOLD (점수 유지)."""

from signal_desk.signals import execution_gate as eg
from signal_desk.signals.engine import SignalResult


def _buy(ticker="T", score=2.0) -> SignalResult:
    return SignalResult(
        ticker=ticker, name=ticker, score=score, kind="BUY", confidence=0.5,
        technical_score=0, fundamental_score=0, has_fundamental=False,
        rank_eligible=True,
    )


def test_late_demotes_buy():
    r = _buy()
    # 에피소드 3일 BUY, 발동 100→지금 140 = late
    hist = {"T": [("2026-07-01", "BUY"), ("2026-07-02", "BUY"), ("2026-07-03", "BUY")]}
    dates = {"T": ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-10"]}
    closes = {"T": [100.0, 105.0, 110.0, 140.0]}
    eg.apply(
        [r], hist_by=hist, dates_by=dates, closes_by=closes,
        events_by={}, today="2026-07-10",
    )
    assert r.kind == "HOLD" and r.gate_blocked and r.rank_eligible is False
    assert r.score == 2.0
    assert any("[추격]" in x for x in r.reasons)


def test_priced_in_demotes_buy():
    r = _buy()
    # 이벤트 전 5거래일 +10%
    ed = "2026-07-20"
    import datetime
    end = datetime.date.fromisoformat(ed)
    dates, closes = [], []
    for i in range(6, 0, -1):
        dates.append((end - datetime.timedelta(days=i)).isoformat())
        t = (6 - i) / 5
        closes.append(100.0 + 10.0 * t)
    dates.append(ed)
    closes.append(120.0)
    for i in range(1, 5):
        dates.append((end + datetime.timedelta(days=i)).isoformat())
        closes.append(120.0 + i)
    events = {"T": [{
        "direction": "positive", "summary": "수주", "effective_at": ed,
    }]}
    eg.apply(
        [r], hist_by={}, dates_by={"T": dates}, closes_by={"T": closes},
        events_by=events, today="2026-07-24",
    )
    assert r.kind == "HOLD" and any("[선반영]" in x for x in r.reasons)


def test_fresh_buy_untouched():
    r = _buy()
    hist = {"T": [("2026-07-24", "BUY")]}
    dates = {"T": ["2026-07-24"]}
    closes = {"T": [100.0]}
    eg.apply(
        [r], hist_by=hist, dates_by=dates, closes_by=closes,
        events_by={}, today="2026-07-24",
    )
    assert r.kind == "BUY" and r.gate_blocked is False
