"""Causal clocks, price-side execution, source gaps and no retrospective promotion."""
import base64
import hashlib
import json

import pytest

from scripts.measure import earnings_announcement_execution as replay


def event(**updates):
    return {"id": "AAPL:2023-02-02", "symbol": "AAPL", "reported_date": "2023-02-02",
            "fiscal_end": "2022-12-31", "ready_ns": replay.clock_ns("2023-02-02T21:31:30Z"),
            "status": "ready", "eligible": True, **updates}


def source(symbol, ns, bid, ask, size=100):
    request = replay.query(symbol, ns)
    stamp = replay.as_dt(ns).isoformat()
    quote = {"t": stamp, "bp": bid, "ap": ask, "bs": size, "as": size,
             "bx": "V", "ax": "V", "c": ["R"]}
    return replay.key(request), {"raw_sha256": "preserved", "body": {"symbol": symbol, "quotes": [quote]},
                                  "has_next_page": False}


def fixture_sources():
    ready = event()["ready_ns"]
    return dict([source("AAPL", ready, 99, 100), source("AAPL", ready + replay.SECOND, 99, 100),
                 source("AAPL", ready + 3601 * replay.SECOND, 102, 103)])


def test_side_prices_fees_cash_and_nanosecond_hold():
    report = replay.simulate({"events": [event()]}, fixture_sources(), "2023", "immediate", "eps")
    assert report["complete"] and report["closed_trades"] == 1
    trade = report["rows"][0]
    assert trade["entry_ask"] == 100 and trade["exit_bid"] == 102
    assert trade["exit_ns"] - trade["entry_ns"] == 3600 * replay.SECOND
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["fees"])
    assert trade["quantity"] * trade["entry_ask"] * 1.001 <= 10_000 / 6
    assert report["actual_historical_net_return_pct"] is None


def test_missing_exit_is_not_zero_pnl_and_position_is_not_deleted():
    index = fixture_sources()
    index.pop(replay.key(replay.query("AAPL", event()["ready_ns"] + 3601 * replay.SECOND)))
    inputs = {"events": [event(), event(id="AAPL:2023-02-03", reported_date="2023-02-03",
                                       ready_ns=event()["ready_ns"] + 86400 * replay.SECOND)]}
    report = replay.simulate(inputs, index, "2023", "immediate", "eps")
    assert report["conditional_account_return_pct"] is None
    assert report["rows"][0]["status"] == "exit_missing_position_unresolved"
    assert report["rows"][1]["status"] == "blocked_by_unresolved_prior_execution"


def test_missing_company_input_keeps_global_denominator():
    inputs = {"events": [event(), event(id="MSFT:2023-01-24", symbol="MSFT", status="news_missing")]}
    report = replay.simulate(inputs, fixture_sources(), "2023", "immediate", "eps")
    assert report["closed_trades"] == 1 and not report["complete"]
    assert report["conditional_account_return_pct"] is None
    assert report["missing_input_events"] == ["MSFT:2023-01-24"]


def test_next_open_is_same_day_for_premarket_and_next_session_for_aftermarket():
    before = event(ready_ns=replay.clock_ns("2023-02-03T12:00:00Z"))
    assert replay.decision(before, "opening", 0) == replay.clock_ns("2023-02-03T14:31:00Z")
    assert replay.decision(event(), "opening", 0) == replay.clock_ns("2023-02-03T14:31:00Z")
    assert replay.decision(event(), "opening", 60) == replay.clock_ns("2023-02-03T14:32:00Z")
    assert replay.query("META", replay.clock_ns("2022-04-27T20:06:00Z"))["symbol"] == "FB"


def test_chronological_selection_not_later_alphabetic_issuer():
    early = event(id="MSFT:2023-02-02", symbol="MSFT")
    later = event(ready_ns=early["ready_ns"] + 30 * replay.SECOND)
    assert replay.choices({"events": [later, early]}, "immediate", "eps")[0]["symbol"] == "MSFT"


def test_revision_not_creation_is_feature_time_and_conflicts_abstain():
    article = {"id": 1, "headline": "Apple Q1 EPS $2.00 Beats $1.80 Estimate",
        "symbols": ["AAPL"], "url": "https://example.com/report",
        "created_at": "2023-02-02T21:00:00Z", "updated_at": "2023-02-02T21:02:00Z"}
    page = {"symbol": "AAPL", "request": {"start": "2023-02-02T00:00:00-05:00"},
            "raw_sha256": "source", "body": {"news": [article], "next_page_token": None}}
    frozen = {"events": [event()]}
    row = replay.input_events(frozen, [page])["events"][0]
    assert row["eligible"] and row["ready_ns"] == replay.clock_ns("2023-02-02T21:02:30Z")
    page["body"]["news"].append({**article, "id": 2, "headline": "Apple Q1 EPS $1.90 Beats $1.80 Estimate"})
    assert replay.input_events(frozen, [page])["events"][0]["status"] == "eps_headline_conflict"
    page["body"]["news"] = [{**article, "updated_at": "2023-02-03T21:02:00Z"}]
    assert replay.input_events(frozen, [page])["events"][0]["status"] == "eps_headline_missing"
    page["body"]["news"] = [{**article, "headline": "Apple Q1 EPS $2.00 vs $1.80 Est."}]
    assert replay.input_events(frozen, [page])["events"][0]["eligible"]
    page["body"]["news"] = [{**article, "headline": "Apple Q1 EPS $2.00 May Not Compare To $1.80 Est."}]
    abstention = replay.input_events(frozen, [page])["events"][0]
    assert abstention["status"] == "ready" and not abstention["eligible"]
    assert abstention["eligibility_reason"] == "comparison_unavailable_abstain"


def test_source_hash_tamper_partial_pages_and_conflicting_quote_versions(tmp_path):
    raw = b'{"news":[]}'
    body = {"schema": "alpaca-historical-news-day-v1", "status": "empty", "raw_bytes": len(raw),
            "raw_b64": base64.b64encode(raw).decode(), "raw_sha256": "incorrect"}
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"rows": [{"gateway_http_status": 200, "body": body}]}))
    with pytest.raises(ValueError, match="hash mismatch"):
        replay.observations([path], "news-day")
    page = {"symbol": "AAPL", "request": {"start": "2023-02-02T00:00:00-05:00"},
            "body": {"news": [], "next_page_token": "missing-next"}}
    assert replay.input_events({"events": [event()]}, [page])["events"][0]["status"] == "news_pagination_incomplete"


def test_locked_quotes_cannot_be_used_and_limit_cannot_be_retroactively_raised():
    index = fixture_sources()
    request = replay.query("AAPL", event()["ready_ns"])
    row = index[replay.key(request)]["body"]["quotes"][0]
    row["bp"] = row["ap"]
    assert replay.first_quote(index, request, event()["ready_ns"]) is None
    index = fixture_sources()
    index[replay.key(replay.query("AAPL", event()["ready_ns"] + replay.SECOND))]["body"]["quotes"][0]["ap"] = 105
    result = replay.simulate({"events": [event()]}, index, "2023", "immediate", "eps")
    assert result["rows"][0]["status"] == "limit_not_marketable" and result["closed_trades"] == 0
