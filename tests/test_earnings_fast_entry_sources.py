"""Execution-source honesty and private evidence preservation, not profitability tests."""

import copy
import datetime as dt
import json

import pytest

from scripts.measure.earnings_fast_entry_sources import (
    SCHEMA, _save, collect_toss, execution_cost_access, minute_access, release_metadata, summary, toss_bar_access,
)


def _bars(day="2023-02-03"):
    opening = dt.datetime.fromisoformat(day + "T09:30:00-05:00")
    rows = [{"timestamp": (opening + dt.timedelta(minutes=i)).isoformat(), "currency": "USD",
             "openPrice": object()} for i in range(62, 0, -1)]
    return {"result": {"candles": rows}}


def _orders():
    return {"result": {"hasNext": True, "orders": [{"orderId": "private-id", "symbol": "AAPL",
        "currency": "USD", "side": "BUY", "status": "FILLED", "orderedAt": "2023-09-01T22:30:00+09:00",
        "execution": {"filledAt": "2023-09-01T22:31:00+09:00", "filledAmount": "1000",
                      "filledQuantity": "10", "averageFilledPrice": "100", "commission": "1",
                      "tax": "0", "settlementDate": None}}]}}


def test_date_only_publication_is_not_replaced_by_modification_or_conference_call():
    raw = b'{"datePublished":"2023-02-02Z","dateModified":"2026-08-19T17:38:13Z"} call 14:00 PT'
    assert release_metadata(raw) == ["2023-02-02Z"]
    assert release_metadata(b'<time datetime="2023-02-22T21:20:00Z">') == ["2023-02-22T21:20:00Z"]


def test_requested_kis_history_is_not_inferred_from_http_success_or_current_bars():
    assert minute_access({"rt_cd": "0", "output2": []}, "20230203")["status"] == "no_bars"
    current = {"rt_cd": "0", "output2": [{"xymd": "20261016", "xhms": "093000", "open": object()}]}
    assert minute_access(current, "20230203")["status"] == "requested_history_not_returned"
    assert minute_access(None, "20230203")["status"] == "provider_failed"


def test_toss_clock_bounds_timezone_and_opening_coverage_are_checked_without_prices():
    body = _bars()
    result = toss_bar_access(body, "2023-02-03T11:00:00-05:00")
    assert result["status"] == "historical_bars_observed"
    assert result["timestamp_semantics"] == "bar_end" and result["quote_execution_eligible"] is False
    for mutated in (_bars("2026-10-16"), {"result": {"candles": [None]}}, _bars("2023-02-02")):
        assert toss_bar_access(mutated, "2023-02-03T11:00:00-05:00")["status"] != "historical_bars_observed"
    body["result"]["candles"].pop()
    assert toss_bar_access(body, "2023-02-03T11:00:00-05:00")["status"] != "historical_bars_observed"
    assert toss_bar_access(_bars(), "2023-02-03T11:00:00")["status"] != "historical_bars_observed"
    assert toss_bar_access(_bars("2026-10-16"), "2026-10-16T11:00:00-05:00")["status"] != "historical_bars_observed"


def test_after_hours_probe_is_not_confused_with_regular_session_or_execution_permission():
    body = _bars()
    for row in body["result"]["candles"]:
        row["timestamp"] = (dt.datetime.fromisoformat(row["timestamp"]) + dt.timedelta(hours=7)).isoformat()
    result = toss_bar_access(body, "2023-02-03T17:40:00-05:00", opening_hour=False)
    assert result["status"] == "historical_bars_observed"
    assert result["window"] == "after_hours_access_probe" and result["quote_execution_eligible"] is False
    assert toss_bar_access(body, "2023-02-03T17:40:00-05:00")["status"] != "historical_bars_observed"


def test_filled_fee_is_not_a_settled_whole_period_fee_schedule_and_ids_stay_private():
    result = execution_cost_access(_orders())
    assert result["accepted_rows"] == 1 and result["settled_rows"] == 0
    assert result["commission_bps_range"] == [10, 10]
    assert result["sides"] == ["BUY"] and result["has_next"] is True
    assert result["full_period_costs_verified"] is False and "private-id" not in json.dumps(result)
    for key, value in (("commission", "NaN"), ("tax", "-1"), ("filledAmount", "1200"),
                       ("settlementDate", "2023-08-01")):
        body = copy.deepcopy(_orders())
        body["result"]["orders"][0]["execution"][key] = value
        assert execution_cost_access(body)["accepted_rows"] == 0


def test_archive_is_private_immutable_and_missing_sources_never_produce_zero_return(tmp_path, monkeypatch):
    archive = tmp_path / "sources.zip"
    files = {"private/toss-orders-2023.json": json.dumps(_orders()).encode()}
    manifest = {"schema": SCHEMA, "credentials_present": {}, "observations": [
        {"source_file": "private/toss-orders-2023.json"}]}
    _save(archive, files, manifest)
    assert archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _save(archive, files, manifest)
    result = summary(archive)
    assert result["net_return_pct"] is None and result["strategy_adoption"] == "undecided"
    assert result["backtest_state"] == "not_run_missing_execution_sources"
    assert not result["live_order_eligible"] and not result["registered_verdict"]
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "_access_token", lambda: pytest.fail("must not make duplicate network requests"))
    with pytest.raises(ValueError, match="already exists"):
        collect_toss(archive, tmp_path / "duplicate.zip")
