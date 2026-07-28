"""호재 선반영 의심 — 이벤트 전 사전 상승 관측."""

import datetime

import pytest

from signal_desk.signals import priced_in as pi


def _series_to_event(
    event_date: str = "2026-07-20",
    *,
    before: int = 6,
    after: int = 4,
    start_px: float = 100.0,
    pre_end_px: float = 108.0,
    event_px: float = 120.0,
) -> tuple[list[str], list[float]]:
    """이벤트 직전 `before`봉 + 이벤트일 + `after`봉. 직전봉=pre_end_px.

    window=5 수익률은 직전봉 기준 start=end-5 이므로 before>=6 필요.
    """
    ed = datetime.date.fromisoformat(event_date)
    dates, closes = [], []
    for i in range(before, 0, -1):
        dates.append((ed - datetime.timedelta(days=i)).isoformat())
        t = (before - i) / max(before - 1, 1)
        closes.append(start_px + (pre_end_px - start_px) * t)
    dates.append(event_date)
    closes.append(event_px)
    for i in range(1, after + 1):
        dates.append((ed + datetime.timedelta(days=i)).isoformat())
        closes.append(event_px + i)
    return dates, closes


def test_pre_event_return_excludes_event_day():
    dates, closes = _series_to_event(start_px=100.0, pre_end_px=108.0)
    ret = pi.pre_event_return_pct(dates, closes, "2026-07-20", 5)
    assert ret == pytest.approx(8.0)  # 108/100 - 1, 이벤트일 120 제외


def test_flag_when_pre_return_above_threshold():
    dates, closes = _series_to_event(start_px=100.0, pre_end_px=110.0)
    ev = {
        "ticker": "A",
        "direction": "positive",
        "event_type": "disclosure_positive",
        "severity": "watch",
        "summary": "대규모 수주",
        "effective_at": "2026-07-20",
    }
    out = pi.compute([ev], dates, closes, today="2026-07-24")
    assert out is not None
    assert out["flag"] is True
    assert out["pre_return_pct"] == 10.0
    assert out["event_date"] == "2026-07-20"
    assert "선반영 의심" in out["tip_ko"]


def test_no_flag_below_threshold():
    dates, closes = _series_to_event(start_px=100.0, pre_end_px=102.0)
    ev = {
        "direction": "positive",
        "summary": "소액 호재",
        "effective_at": "2026-07-20",
    }
    assert pi.compute([ev], dates, closes, today="2026-07-24") is None


def test_ignores_negative_direction():
    dates, closes = _series_to_event(start_px=100.0, pre_end_px=110.0)
    ev = {
        "direction": "negative",
        "summary": "악재",
        "effective_at": "2026-07-20",
    }
    assert pi.compute([ev], dates, closes, today="2026-07-24") is None


def test_ignores_stale_event():
    dates, closes = _series_to_event(
        "2026-07-01", start_px=100.0, pre_end_px=110.0, after=25,
    )
    ev = {
        "direction": "positive",
        "summary": "옛 호재",
        "effective_at": "2026-07-01",
    }
    assert pi.compute([ev], dates, closes, today="2026-07-24") is None


def test_event_anchor_prefers_effective_at():
    ts = int(datetime.datetime(2026, 7, 20, 9, 0, tzinfo=pi._KST).timestamp())
    assert pi.event_anchor_date({"effective_at": ts, "detected_at": 1}) == "2026-07-20"
    assert pi.event_anchor_date({"detected_at": ts}) == "2026-07-20"
    assert pi.event_anchor_date({"effective_at": "2026-07-18T12:00:00"}) == "2026-07-18"


def test_annotate_rows():
    dates_a, closes_a = _series_to_event(start_px=100.0, pre_end_px=110.0)
    dates_b, closes_b = _series_to_event(start_px=100.0, pre_end_px=100.0)
    rows = [{"ticker": "A"}, {"ticker": "B"}]
    events = {
        "A": [{"direction": "positive", "summary": "수주", "effective_at": "2026-07-20"}],
        "B": [{"direction": "positive", "summary": "수주", "effective_at": "2026-07-20"}],
    }
    pi.annotate_rows(
        rows,
        events_by=events,
        dates_by={"A": dates_a, "B": dates_b},
        closes_by={"A": closes_a, "B": closes_b},
        today="2026-07-24",
    )
    assert rows[0]["priced_in"]["flag"] is True
    assert rows[1]["priced_in"] is None
