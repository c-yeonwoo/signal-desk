"""전진 관점 비교는 당시 첫 판단과 미래 완료 종가만 사용한다."""

import datetime as dt

import pytest

from signal_desk import db
from signal_desk.signals import lens_forward


def _stamp(day: str, hour: int = 10) -> int:
    return int(dt.datetime.fromisoformat(day).replace(hour=hour,
               tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp())


def _cohort(day="2026-09-28", *, event="pass", entry="pass", ticker="AAA"):
    observed = _stamp(day)
    return {"iso_week": lens_forward.iso_week("kr", observed),
            "snapshot_id": "s-" + day, "observed_at": observed,
            "snapshot": {"market": "kr", "mode": "read_only", "rows": [
                {"ticker": ticker, "kind": "BUY", "rank": 1,
                 "lenses": {"quant": {"verdict": "pass", "as_of": "2026-09-23"},
                            "event": {"verdict": event}, "entry": {"verdict": entry}}}]}}


def test_weekly_first_snapshot_is_immutable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for sid in ("first", "later"):
        assert db.lens_snapshot_put({"id": sid, "market": "kr", "version": "v",
                                    "observed_at": _stamp("2026-09-28"), "mode": "read_only",
                                    "order_eligible": False, "rows": []})
    assert db.lens_forward_cohort_freeze("kr", "2026-W40", "first", _stamp("2026-09-28"))
    assert not db.lens_forward_cohort_freeze("kr", "2026-W40", "later", _stamp("2026-09-29"))
    assert db.lens_forward_cohorts("kr")[0]["snapshot_id"] == "first"
    assert db.lens_forward_cohort_freeze("us", "2026-W40", "later", _stamp("2026-09-29"))


def test_future_close_required_and_cash_denominator_not_reweighted():
    cohort = _cohort(event="unavailable")
    prices = lambda _: [{"date": "2026-09-29", "close": 100},
                        {"date": "2026-10-07", "close": 110}]
    before = lens_forward.evaluate([cohort], "kr", prices,
              now=dt.datetime(2026, 10, 7, 5, tzinfo=dt.timezone.utc))
    assert before["independent_episodes"] == 0
    assert before["exclusions"]["성과 관측 대기"] == 1
    after = lens_forward.evaluate([cohort], "kr", prices,
              now=dt.datetime(2026, 10, 7, 8, tzinfo=dt.timezone.utc))
    assert after["independent_episodes"] == 1
    assert after["summary"]["base"]["mean_net_return"] == pytest.approx(.0096)
    assert after["summary"]["event"]["mean_net_return"] == 0
    assert after["summary"]["entry"]["mean_net_return"] == pytest.approx(.0096)
    assert after["summary"]["base"]["mean_coverage"] == .1
    assert after["live_eligible"] is False


def test_missing_price_and_stale_input_fail_closed():
    cohort = _cohort()
    now = dt.datetime(2026, 10, 8, 9, tzinfo=dt.timezone.utc)
    missing = lens_forward.evaluate([cohort], "kr", lambda _: [], now=now)
    assert missing["exclusions"]["진입·청산 종가 누락"] == 1
    cohort["snapshot"]["rows"][0]["lenses"]["quant"]["as_of"] = "2026-09-24"
    stale = lens_forward.evaluate([cohort], "kr", lambda _: [], now=now)
    assert stale["exclusions"]["당시 유효 매수 후보 없음"] == 1


def test_overlapping_weeks_are_not_independent():
    first = _cohort()
    second = _cohort("2026-10-01")
    # 같은 주 첫 조회끼리는 DB가 하나만 받지만, 순수 평가기도 겹침을 거른다.
    prices = lambda _: [{"date": "2026-09-29", "close": 100},
                        {"date": "2026-10-07", "close": 101}]
    result = lens_forward.evaluate([first, second], "kr", prices,
             now=dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc))
    assert result["independent_episodes"] == 1
    assert result["exclusions"]["보유 구간 겹침"] == 1


def test_first_observed_price_marks_survive_source_revision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = _cohort()
    now = dt.datetime(2026, 10, 8, 9, tzinfo=dt.timezone.utc)
    def prices(end):
        return lambda _: [{"date": "2026-09-29", "close": 100},
                          {"date": "2026-10-07", "close": end}]
    first = lens_forward.evaluate([cohort], "kr", prices(110), now=now,
                                  price_marker=db.lens_forward_mark)
    revised = lens_forward.evaluate([cohort], "kr", prices(999), now=now,
                                    price_marker=db.lens_forward_mark)
    assert first["summary"]["base"]["mean_net_return"] == revised["summary"]["base"]["mean_net_return"]
    assert db.lens_forward_mark("kr", cohort["snapshot_id"], "AAA", "2026-10-07", None) == 110
