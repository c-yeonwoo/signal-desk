"""조회용 렌즈는 같은 입력을 비교하고 기본 시그널·주문을 바꾸지 않는다."""

import copy

import pytest

from signal_desk import api, db
from signal_desk.signals import lenses


def _row(ticker="AAA", **overrides):
    row = {"ticker": ticker, "name": ticker, "kind": "BUY", "score": 1.5,
           "price": 100.0, "rank": 1, "data_coverage": 0.9,
           "factor_scores": {"momentum": 0.5}, "decision_buy_blocked": False,
           "gate_blocked": False,
           "entry": {"quality": "fresh", "fire_date": "2026-09-28"},
           "priced_in": None}
    return {**row, **overrides}


def test_same_snapshot_id_replays_and_new_input_changes_id():
    first = [_row()]
    original = copy.deepcopy(first)
    a = lenses.build_snapshot(first, market="kr", signal_policy_id="policy-a",
                              dates_by={"AAA": ["2026-09-29"]}, observed_at=100)
    b_rows = [_row()]
    b = lenses.build_snapshot(b_rows, market="kr", signal_policy_id="policy-a",
                              dates_by={"AAA": ["2026-09-29"]}, observed_at=200)
    assert a["id"] == b["id"]
    assert a["observed_at"] != b["observed_at"]
    assert first[0]["kind"] == original[0]["kind"] and first[0]["score"] == original[0]["score"]
    assert a["order_eligible"] is False and a["mode"] == "read_only"
    changed = lenses.build_snapshot([_row(price=101)], market="kr", signal_policy_id="policy-a",
                                    dates_by={"AAA": ["2026-09-29"]}, observed_at=200)
    assert changed["id"] != a["id"]


def test_missing_event_is_unknown_not_good_news():
    rows = [_row()]
    snap = lenses.build_snapshot(rows, market="kr", signal_policy_id="p",
                                 dates_by={"AAA": ["2026-09-29"]}, events=[])
    assert rows[0]["lens_results"]["event"]["verdict"] == "unavailable"
    assert "안전하다는 뜻이 아님" in rows[0]["lens_results"]["event"]["reason"]
    assert snap["rows"][0]["lenses"]["quant"]["verdict"] == "pass"
    assert rows[0]["lens_results"]["entry"]["verdict"] == "pass"


def test_blocked_event_is_not_overridden_by_positive_or_timing_lens():
    rows = [_row(decision_buy_blocked=True)]
    event = {"id": 12, "ticker": "AAA", "direction": "positive", "trust_tier": "official",
             "detected_at": 1790672400, "expires_at": 1791072400}
    lenses.build_snapshot(rows, market="kr", signal_policy_id="p",
                          dates_by={"AAA": ["2026-09-29"]}, events=[event])
    assert rows[0]["kind"] == "BUY"  # 서버의 원래 kind를 바꾸지 않는다.
    assert rows[0]["lens_results"]["event"]["verdict"] == "exclude"
    assert rows[0]["lens_results"]["quant"]["verdict"] == "exclude"
    assert rows[0]["lens_results"]["entry"]["verdict"] == "pass"
    assert all(r["research_only"] for r in rows[0]["lens_results"].values())


def test_confirmed_but_not_decision_eligible_event_is_not_a_pass():
    event = {"id": 13, "ticker": "AAA", "direction": "positive", "trust_tier": "official",
             "decision_eligible": False, "detected_at": 1790672400, "expires_at": 1791072400}
    rows = [_row()]
    lenses.build_snapshot(rows, market="kr", signal_policy_id="p",
                          dates_by={"AAA": ["2026-09-29"]}, events=[event])
    result = rows[0]["lens_results"]["event"]
    assert result["verdict"] == "unavailable"
    assert result["evidence_ids"] == ["event:13"]
    event["decision_eligible"] = True
    approved_rows = [_row()]
    lenses.build_snapshot(approved_rows, market="kr", signal_policy_id="p",
                          dates_by={"AAA": ["2026-09-29"]}, events=[event])
    assert approved_rows[0]["lens_results"]["event"]["verdict"] == "pass"


def test_no_price_asof_means_unknown_even_if_score_is_high():
    rows = [_row(score=3.0)]
    lenses.build_snapshot(rows, market="us", signal_policy_id="p", dates_by={})
    assert rows[0]["lens_results"]["quant"]["verdict"] == "unavailable"
    assert rows[0]["lens_results"]["entry"]["verdict"] == "unavailable"


def test_snapshot_ledger_is_immutable_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = lenses.build_snapshot([_row()], market="kr", signal_policy_id="p",
                                     dates_by={"AAA": ["2026-09-29"]}, observed_at=100)
    assert db.lens_snapshot_put(original) is True
    assert db.lens_snapshot_put(original) is False
    saved = db.lens_snapshot_get(original["id"])
    assert saved == original
    altered = {**original, "order_eligible": True}
    with pytest.raises(ValueError):
        db.lens_snapshot_put(altered)
    assert db.lens_snapshot_get(original["id"]) == original


def test_lens_failure_does_not_break_main_signal(monkeypatch):
    rows = [_row()]
    monkeypatch.setattr(api.store, "load_dates_by_ticker", lambda: {"AAA": ["2026-09-29"]})
    monkeypatch.setattr(api.db, "kb_events_active", lambda: [])
    monkeypatch.setattr(api.lenses, "build_snapshot", lambda *args, **kwargs: 1 / 0)
    meta = api._attach_lens_snapshot(rows, market="kospi", signal_policy_id="p")
    assert meta["ready"] is False and rows[0]["kind"] == "BUY"
    assert "lens_results" not in rows[0]
