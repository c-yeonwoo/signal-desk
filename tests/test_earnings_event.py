"""Financial basis, as-of disclosure ordering, and shared execution contracts."""

import datetime as dt

import pytest

from scripts.measure.contract_event_sources import _rows
from scripts.measure.earnings_event_backtest import VALIDATION, verdict
from signal_desk import market_clock
from signal_desk.signals.earnings_event import execution_event, facts, replay, select_events, trading_clock


def _guidance(value="100", note="", start="2026-01-01", end="2026-12-31"):
    return [["1. 연결 영업실적 전망 내용"], ["구분(단위 : 억원)", "2026", "사업연도"],
            ["대상기간", "시작일", start], ["종료일", end], ["매출액", value],
            ["영업이익", "-"], ["수주 (단위: 미화 백만불)", "500"],
            ["2. 전망 또는 예측의 근거", "경영계획"], [note]]


def _event(receipt, stamp, kind="guidance", **kwargs):
    return {"receipt": receipt, "ticker": "005930", "issuer": "삼성전자", "kind": kind,
            "published_at": stamp + "+09:00", "session": stamp[:10], "status": "original_preserved",
            "source_sha256": receipt, **kwargs}


def test_revenue_uses_disclosed_range_not_midpoint_and_ignores_usd_order_metric():
    point = facts(_guidance("100", "매출액: 90 억 ~ 110 억원 전망"), "guidance")[0]
    assert point["revenue_low_krw"] == 9_000_000_000
    assert point["revenue_high_krw"] == 11_000_000_000
    assert point["scope"] == "consolidated"
    bounded = facts(_guidance("100", "매출액: 약 100억원 (범위 : 90억원 ~ 110억원)"), "guidance")[0]
    assert bounded == point
    with pytest.raises(ValueError, match="range/period"):
        facts(_guidance("100", "매출 전망 +10% ~ +20%"), "guidance")


def test_corrected_table_and_numbered_notes_do_not_override_current_financial_section():
    rows = [["정정사유", "연간 매출 전망 수정"], ["1. 연결 영업실적 전망 내용", "매출액:90", "매출액:100"]]
    rows += _guidance(note="1. 상기 1. 연결 영업실적 전망 내용은 예측임")
    assert facts(rows, "guidance")[0]["revenue_high_krw"] == 10_000_000_000
    rows[0][1] = "단위 기재 오류 수정"
    with pytest.raises(ValueError, match="clerical correction"):
        facts(rows, "guidance")


def test_actual_uses_current_explicit_period_not_cumulative_previous_or_filing_date():
    rows = [["당기실적", "2026-04-01", "~", "2026-06-30"],
            ["당기누계실적", "2026-01-01", "~", "2026-06-30"],
            ["1. 연결실적내용", "단위 : 백만원, %"],
            ["구분", "당기실적", "전기실적"], ["(2026.2Q)", "(2026.1Q)"],
            ["매출액", "당해실적", "10000", "9000"], ["누계실적", "50000", "9000"],
            ["영업이익", "당해실적", "100", "50"], ["2. 정보제공내역", "IR"]]
    point = facts(rows, "earnings")[0]
    assert point["start"] == "2026-04-01" and point["end"] == "2026-06-30"
    assert point["revenue_low_krw"] == 10_000_000_000
    rows[4][0] = "(2026.1Q)"
    with pytest.raises(ValueError, match="header conflict"):
        facts(rows, "earnings")


def test_future_actual_period_and_management_adjusted_perimeter_are_not_buy_evidence():
    with pytest.raises(ValueError, match="reporting perimeter"):
        facts(_guidance(note="자회사를 제외한 관리연결 기준"), "guidance")
    prior = _event("p", "2026-01-05T10:00:00")
    actual = _event("a", "2026-02-02T10:00:00", kind="earnings")
    basis = facts(_guidance(), "guidance")
    later = [{**basis[0], "revenue_low_krw": 20_000_000_000, "operating_profit_krw": 1}]
    events, audit = select_events([prior, actual], {"p": basis, "a": later}, market_clock.is_session)
    assert not events and audit[-1]["reason"] == "actual_period_has_not_closed"


def test_same_company_period_and_conservative_range_must_really_improve():
    first = _event("p", "2026-01-05T10:00:00")
    second = _event("n", "2026-02-02T10:00:00")
    prior = facts(_guidance("100", "매출액: 90~110억원"), "guidance")
    current = facts(_guidance("110"), "guidance")
    events, audit = select_events([first, second], {"p": prior, "n": current}, market_clock.is_session)
    assert not events and audit[-1]["reason"] == "below_fixed_five_percent_upgrade"
    current = facts(_guidance("120"), "guidance")
    events, _ = select_events([first, second], {"p": prior, "n": current}, market_clock.is_session)
    assert events[0]["prior_receipt"] == "p"
    assert events[0]["improvement_pct"] == pytest.approx((120/110-1)*100)
    current[0]["scope"] = "separate"
    assert not select_events([first, second], {"p": prior, "n": current}, market_clock.is_session)[0]


def test_fresh_unknown_or_same_day_guidance_cannot_be_skipped_for_older_easier_basis():
    old = _event("old", "2026-01-05T10:00:00")
    fresh = _event("fresh", "2026-02-02T09:20:00")
    event = _event("new", "2026-02-02T10:00:00")
    points = {"old": facts(_guidance("100"), "guidance"), "new": facts(_guidance("120"), "guidance")}
    events, audit = select_events([old, fresh, event], points, market_clock.is_session)
    assert not events and audit[-1]["reason"] == "ambiguous_intervening_guidance"
    points["fresh"] = facts(_guidance("104"), "guidance")
    events, audit = select_events([old, fresh, event], points, market_clock.is_session)
    assert not events and audit[-1]["reason"] == "latest_guidance_not_at_least_one_day_old"


def test_after_close_waits_over_weekend_without_renaming_the_publication():
    event = _event("n", "2026-01-30T16:00:00")
    clock = trading_clock(event)
    assert clock["execution_session"] == "2026-02-02"
    assert clock["trade_anchor_at"] == "2026-02-02T09:08:00+09:00"
    adapted = execution_event({**event, **clock})
    assert adapted["official_published_at"] == event["published_at"]
    assert adapted["session"] == "2026-02-02"
    with pytest.raises(ValueError, match="beyond frozen"):
        trading_clock(_event("n", "2026-07-31T16:00:00"))


def test_new_financial_publication_before_decision_invalidates_the_old_event():
    prior = _event("p", "2026-01-05T10:00:00")
    positive = _event("n", "2026-02-02T10:00:00")
    correction = _event("c", "2026-02-02T10:01:00")
    points = {"p": facts(_guidance(), "guidance"), "n": facts(_guidance("120"), "guidance")}
    selected, audit = select_events([prior, positive, correction], points, market_clock.is_session)
    assert not selected and audit[1]["reason"] == "newer_financial_publication_before_decision"


def test_baseline_year_seeds_expectations_but_cannot_become_a_trade():
    prior = _event("p", "2025-12-01T10:00:00", source_group="baseline_guidance")
    event = _event("n", "2026-02-02T10:00:00")
    points = {"p": facts(_guidance(), "guidance"), "n": facts(_guidance("120"), "guidance")}
    selected, audit = select_events([prior, event], points, market_clock.is_session)
    assert len(selected) == 1 and selected[0]["receipt"] == "n"
    assert audit[0]["reason"] == "baseline_only_not_an_event"


def test_absent_validation_is_not_zero_return_or_permission_to_trade():
    months = {month: {"75bp_full_delay0": {"fundamental": {"state": "no_eligible_events"}}} for month in VALIDATION}
    result = verdict(months)
    assert result["adoption"] == "rejected"
    assert result["reason"] == "insufficient_or_incomplete_validation"
    assert result["live_order_eligible"] is False
