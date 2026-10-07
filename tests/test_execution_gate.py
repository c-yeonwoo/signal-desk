"""실행 게이트 — late·선반영이면 BUY→HOLD (점수 유지)."""

import pytest

from signal_desk import db, store
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


@pytest.mark.parametrize("market", ["kospi", "us"])
def test_gate_uses_evaluated_price_bundle_without_reloading(market, monkeypatch):
    def unexpected_reload():
        raise AssertionError("gate must not reread the evaluated prices")

    monkeypatch.setattr(store, "load_signal_history", lambda: [])
    monkeypatch.setattr(db, "kb_events_active", lambda: [])
    monkeypatch.setattr(store, "load_price_series", unexpected_reload)
    monkeypatch.setattr(store, "load_us_price_series", unexpected_reload)
    monkeypatch.setattr(store, "load_dates_by_ticker", unexpected_reload)
    monkeypatch.setattr(store, "load_us_dates_by_ticker", unexpected_reload)
    seen = {}

    def capture(_ticker, *, price, dates, closes, **_kwargs):
        seen.update(price=price, dates=dates, closes=closes)
        return None

    monkeypatch.setattr(eg.entry_quality, "compute", capture)
    r = _buy()
    eg.apply_from_store([r], market=market, today="2026-10-07",
                        price_bundle=({"T": [100.0, 111.0]},
                                      {"T": ["2026-10-06", "2026-10-07"]}))

    assert seen == {"price": 111.0, "dates": ["2026-10-06", "2026-10-07"],
                    "closes": [100.0, 111.0]}
    assert r.kind == "BUY"


def test_gate_capture_records_actual_inputs_and_failure_status(monkeypatch):
    monkeypatch.setattr(store, "load_signal_history", lambda: object())
    monkeypatch.setattr(eg.entry_quality, "history_kinds_by_ticker",
                        lambda _history: {"T": [("2026-10-06", "BUY")]})
    monkeypatch.setattr(db, "kb_events_active", lambda: [{"ticker": "T", "direction": "positive"}])
    monkeypatch.setattr(eg.priced_in, "events_by_ticker",
                        lambda _events: {"T": [{"direction": "positive"}]})
    prices, dates = {"T": [100.0]}, {"T": ["2026-10-06"]}
    captured = {}
    eg.apply_from_store([_buy()], today="2026-10-07", price_bundle=(prices, dates),
                        capture=captured)
    assert captured["status"] == "applied"
    assert captured["closes_by"] is prices and captured["dates_by"] is dates
    assert captured["hist_by"]["T"] == [("2026-10-06", "BUY")]
    assert captured["events_by"]["T"][0]["direction"] == "positive"

    monkeypatch.setattr(db, "kb_events_active", lambda: 1 / 0)
    failed = {}
    eg.apply_from_store([_buy()], today="2026-10-07", price_bundle=(prices, dates),
                        capture=failed)
    assert failed["status"] == "failed_partial" and failed["error_type"] == "ZeroDivisionError"


def test_empty_gate_capture_has_explicit_day_and_price_bundle():
    prices, dates = {"T": [100.0]}, {"T": ["2026-10-06"]}
    captured = {}
    assert eg.apply_from_store([], today="2026-10-07", price_bundle=(prices, dates),
                               capture=captured) == []
    assert captured["status"] == "empty" and captured["today"] == "2026-10-07"
    assert captured["closes_by"] is prices and captured["dates_by"] is dates
