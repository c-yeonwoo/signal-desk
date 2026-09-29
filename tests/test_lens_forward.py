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
    assert stale["exclusions"]["매수 후보 가격 기준 불일치"] == 1


def test_no_buy_signal_is_a_cash_week_not_a_missing_week():
    cohort = _cohort()
    cohort["snapshot"]["rows"][0]["kind"] = "HOLD"
    result = lens_forward.evaluate([cohort], "kr", lambda _: 1 / 0,
              now=dt.datetime(2026, 10, 8, 9, tzinfo=dt.timezone.utc))
    assert result["independent_episodes"] == 1
    assert result["episodes"][0]["candidate_count"] == 0
    assert result["summary"]["base"]["mean_net_return"] == 0


def test_missing_scheduled_week_is_reported_not_backfilled():
    cohort = _cohort()
    result = lens_forward.evaluate([cohort], "kr", lambda _: [],
             now=dt.datetime(2026, 10, 8, 9, tzinfo=dt.timezone.utc),
             capture_source="scheduled", expected_weeks=["2026-W40", "2026-W41"])
    assert result["capture_coverage"] == .5
    assert result["missing_capture_weeks"] == ["2026-W41"]


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
    unmarked = lens_forward.evaluate([cohort], "kr", prices(110), now=now,
                                     price_marker=db.lens_scheduled_price_mark)
    assert unmarked["exclusions"]["진입·청산 정시 표식 누락"] == 1
    # 예전 관리자 조회 원장은 별도 보존하지만 새 정시 연구의 가격으로 승격하지 않는다.
    db.lens_forward_mark("kr", cohort["snapshot_id"], "AAA", "2026-09-29", 100)
    db.lens_forward_mark("kr", cohort["snapshot_id"], "AAA", "2026-10-07", 999)
    still_unmarked = lens_forward.evaluate([cohort], "kr", prices(110), now=now,
                                           price_marker=db.lens_scheduled_price_mark)
    assert still_unmarked["independent_episodes"] == 0
    entry_day = dt.datetime(2026, 9, 29, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    exit_day = dt.datetime(2026, 10, 7, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    entry = lens_forward.collect_price_marks([cohort], "kr", prices(110), db.lens_scheduled_price_mark,
                                             now=entry_day)
    exit_mark = lens_forward.collect_price_marks([cohort], "kr", prices(110), db.lens_scheduled_price_mark,
                                                 now=exit_day)
    assert entry["marked"] == 1 and exit_mark["marked"] == 1
    first = lens_forward.evaluate([cohort], "kr", lambda _: 1 / 0, now=now,
                                  price_marker=db.lens_scheduled_price_mark)
    lens_forward.collect_price_marks([cohort], "kr", prices(999), db.lens_scheduled_price_mark,
                                     now=exit_day)
    revised = lens_forward.evaluate([cohort], "kr", lambda _: 1 / 0, now=now,
                                    price_marker=db.lens_scheduled_price_mark)
    assert first["summary"]["base"]["mean_net_return"] == revised["summary"]["base"]["mean_net_return"]
    assert db.lens_scheduled_price_mark("kr", cohort["snapshot_id"], "AAA", "2026-10-07", None) == 110


def test_missed_mark_window_cannot_be_backfilled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = _cohort()
    prices = lambda _: [{"date": "2026-09-29", "close": 100},
                        {"date": "2026-10-07", "close": 110}]
    late = dt.datetime(2026, 10, 8, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    status = lens_forward.collect_price_marks([cohort], "kr", prices, db.lens_scheduled_price_mark, now=late)
    assert status["marked"] == 0
    assert db.lens_scheduled_price_mark("kr", cohort["snapshot_id"], "AAA", "2026-10-07", None) is None


def test_later_source_revision_halts_episode_without_rewriting_mark(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = _cohort()
    sid = cohort["snapshot_id"]
    db.lens_scheduled_price_mark("kr", sid, "AAA", "2026-09-29", 100)
    db.lens_scheduled_price_mark("kr", sid, "AAA", "2026-10-07", 110)
    marks = db.lens_scheduled_price_marks("kr")
    now = dt.datetime(2026, 10, 8, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    prices = lambda _: [{"date": "2026-09-29", "close": 100},
                        {"date": "2026-10-07", "close": 55}]
    status = lens_forward.audit_price_revisions(marks, "kr", prices,
                                                 db.lens_scheduled_price_halt_add, now=now)
    assert status["checked"] == 2 and status["new_halts"] == 1
    assert db.lens_scheduled_price_mark("kr", sid, "AAA", "2026-10-07", None) == 110
    again = lens_forward.audit_price_revisions(marks, "kr", prices,
                                                db.lens_scheduled_price_halt_add, now=now)
    assert again["new_halts"] == 0
    result = lens_forward.evaluate([cohort], "kr", lambda _: 1 / 0, now=now,
                                   price_marker=db.lens_scheduled_price_mark,
                                   halted_marks=db.lens_scheduled_price_halt_keys("kr"))
    assert result["independent_episodes"] == 0
    assert result["exclusions"]["원천 가격 수정 감지"] == 1
    assert db.lens_scheduled_price_halt_keys("kr") == {(sid, "AAA", "2026-10-07")}


def test_revision_audit_ignores_same_day_and_unavailable_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.lens_scheduled_price_mark("kr", "s", "AAA", "2026-10-08", 100)
    db.lens_scheduled_price_mark("kr", "s", "BBB", "2026-10-07", 100)
    now = dt.datetime(2026, 10, 8, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    status = lens_forward.audit_price_revisions(db.lens_scheduled_price_marks("kr"), "kr",
              lambda _: [{"date": "2026-10-08", "close": 50}],
              db.lens_scheduled_price_halt_add, now=now)
    assert status["source_missing"] == 1 and status["new_halts"] == 0
    assert db.lens_scheduled_price_halt_keys("kr") == set()
    with pytest.raises(ValueError, match="provenance"):
        db.lens_scheduled_price_halt_add("kr", "s", "BBB", "2026-10-07", 999, 50)


def test_overlapping_interval_not_selected_based_on_price_gap():
    first = _cohort()
    second = _cohort("2026-10-01")
    first["snapshot"]["rows"][0]["ticker"] = "NO_PRICE"
    second["snapshot"]["rows"][0]["ticker"] = "HAS_PRICE"
    prices = lambda ticker: [] if ticker == "NO_PRICE" else [
        {"date": "2026-10-02", "close": 100}, {"date": "2026-10-12", "close": 110}]
    result = lens_forward.evaluate([first, second], "kr", prices,
                                   now=dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc))
    assert result["independent_episodes"] == 0
    assert result["exclusions"]["진입·청산 종가 누락"] == 1
    assert result["exclusions"]["보유 구간 겹침"] == 1
