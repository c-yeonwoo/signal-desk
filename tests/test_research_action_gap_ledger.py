"""The gap ledger cannot infer a clean corporate-action day from raw prices."""

import datetime as dt
import json

import pytest

from scripts.measure import research_action_gap_ledger as ledger


def test_top200_matches_raw_pilot_universe_and_rejects_thin_panel():
    rows = [{"ISU_CD": f"{index:05d}0", "ISU_NM": str(index),
             "MKTCAP": str(1_000 - index)} for index in range(201)]
    rows += [{"ISU_CD": "123455", "MKTCAP": "999999"},
             {"ISU_CD": "888880", "MKTCAP": "not-a-number"}]
    ranked = ledger._top200(rows)
    assert len(ranked) == 200
    assert ranked[0][0] == "000000" and ranked[-1][0] == "001990"
    with pytest.raises(ValueError, match="fewer than 200"):
        ledger._top200(rows[:199])


def test_all_candidate_tickers_retained_and_post_last_price_not_zero_return(monkeypatch, tmp_path):
    sessions = [(dt.date(2024, 4, 1) + dt.timedelta(days=offset)).isoformat()
                for offset in range(23)]
    days = {}
    for index, day in enumerate(sessions):
        first = 0 if index == 0 else 1
        days[day] = [{"ISU_CD": f"{ticker:05d}0", "ISU_NM": str(ticker),
                      "MKTCAP": str(1_000 - ticker), "LIST_SHRS": "1000"}
                     for ticker in range(first, 201)]
    # One candidate vanishes after the first decision. It remains in the
    # denominator, even though it was never a simulated BUY.
    monkeypatch.setattr(ledger, "_frozen_windows", lambda _: [
        ("test", sessions[0], sessions[1], ("raw.zip",))])
    monkeypatch.setattr(ledger, "load_krx_archives", lambda _: (
        days, [{"archive": "raw.zip", "sha256": "a" * 64}]))
    monkeypatch.setattr(ledger, "SignalConfig", lambda: type("C", (), {
        "ma_long": 2, "momentum_lookback": 1})())
    monkeypatch.setattr(ledger.market_clock, "next_sessions", lambda market, day, count: (
        sessions[sessions.index(day) + 1:sessions.index(day) + 1 + count]))
    reviewed = tmp_path / "links.json"
    reviewed.write_text(json.dumps({"schema": "research-action-reviewed-links-v1",
                                    "raw_archived": False, "cases": [{
        "ticker": "000000", "event_date": "2024-04-03",
        "source_url": "https://kind.krx.co.kr/test",
        "economic_settlement_confirmed": False, "pit_eligible": False}]}),
                        encoding="utf-8")
    report = ledger.build_ledger(tmp_path, reviewed_cases=reviewed)
    assert len(report["ticker_rows"]) == 201
    vanished = next(row for row in report["ticker_rows"] if row["ticker"] == "000000")
    assert vanished["candidate_sessions"] == 1
    assert vanished["missing_after_last_price"] == 21
    assert vanished["action_coverage"] == "partial"
    assert vanished["reviewed_url_only_cases"][0]["event_date"] == "2024-04-03"
    assert "post_last_price_cause_unverified" in vanished["gaps"]
    assert report["verified_total_return"] is False
    reviewed.write_text(reviewed.read_text().replace('"pit_eligible": false',
                                                    '"pit_eligible": true'), encoding="utf-8")
    with pytest.raises(ValueError, match="overclaimed"):
        ledger.build_ledger(tmp_path, reviewed_cases=reviewed)


def test_frozen_pilot_hash_cannot_be_silently_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "PILOTS", {"frozen": ("pilot.json", "a" * 64)})
    (tmp_path / "pilot.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen pilot SHA"):
        ledger._frozen_windows(tmp_path)


def test_checked_in_gap_inventory_retains_hard_cases_without_promoting_returns():
    path = ledger.ROOT / "docs/research-action-gap-ledger-2026-10-11.jsonl"
    saved, *ticker_rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert saved["type"] == "manifest" and all(row["type"] == "ticker_gap" for row in ticker_rows)
    assert len(saved["windows"]) == 7 and len(ticker_rows) == 1488
    assert saved["verified_total_return"] is False
    rows = {(row["window"], row["ticker"]): row for row in ticker_rows}
    assert rows["2024-jun-jul", "003410"]["missing_after_last_price"] == 21
    assert rows["2025q4-v5", "010620"]["missing_after_last_price"] == 21
    assert rows["2026q1-v5", "042670"]["missing_after_last_price"] == 21
    assert rows["2026jun-expanded-v5", "012510"]["missing_after_last_price"] == 11
    assert all(row["action_coverage"] == "partial" for row in ticker_rows)
    assert saved["schedule_comparisons"][0]["state"] == "matched_schedule_fields"
    assert saved["schedule_comparisons"][0]["actual_payment_confirmed"] is False
