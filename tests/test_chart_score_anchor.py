"""차트 점수의 '오늘' 점을 현재 시그널 점수(전 팩터)로 앵커링 — 리스트 점수와 최신값 일치."""

from types import SimpleNamespace

from signal_desk import api


def test_anchor_kr_uses_current_signal_score(monkeypatch):
    monkeypatch.setattr(api, "_signals",
                        lambda: [SimpleNamespace(ticker="005930", score=1.87),
                                 SimpleNamespace(ticker="000660", score=-0.4)])
    out = api._anchor_today_score([0.1, 0.2, 0.3], "005930", "kospi")
    assert out[-1] == 1.87 and out[:-1] == [0.1, 0.2]  # 과거는 그대로, 오늘만 시그널 점수


def test_anchor_us_uses_dict_lookup(monkeypatch):
    monkeypatch.setattr(api, "_us_signals", lambda: {"AAPL": SimpleNamespace(ticker="AAPL", score=2.1)})
    out = api._anchor_today_score([0.0, None], "AAPL", "us")
    assert out[-1] == 2.1


def test_anchor_noop_when_ticker_missing(monkeypatch):
    monkeypatch.setattr(api, "_signals", lambda: [SimpleNamespace(ticker="XXX", score=1.0)])
    out = api._anchor_today_score([0.5, 0.6], "005930", "kospi")
    assert out == [0.5, 0.6]  # 못 찾으면 원본 유지


def test_anchor_empty_series():
    assert api._anchor_today_score([], "005930", "kospi") == []
