"""Causal shortlist, executable timing and missing-position accounting contracts."""
import base64
import hashlib
import json

import pytest

from scripts.measure import intraday_orb_replay as orb
from signal_desk.ingest import alpaca_research


@pytest.fixture
def panel():
    frozen = orb.plan()
    sessions = frozen["sessions"][:21]
    sources = {}
    for index, day in enumerate(sessions):
        opening, _ = orb.session_clocks(day)
        for rank, symbol in enumerate(orb.SYMBOLS):
            volume = 100 * (rank + 1) if index < 20 else 100 * (8 - rank) * (rank + 1)
            bars = [{"t": opening + j * orb.MINUTE, "o": 100., "h": 100.5, "l": 99.5,
                     "c": 100.2 if index == 20 else 100., "v": volume, "vw": 100.}
                    for j in range(5)]
            if index == 20:
                bars += [{"t": opening + 5 * orb.MINUTE, "o": 100.2, "h": 100.7,
                          "l": 100.1, "c": 100.6, "v": 100, "vw": 100.4},
                         {"t": opening + 6 * orb.MINUTE, "o": 100.6, "h": 103.,
                          "l": 100.5, "c": 102.9, "v": 100, "vw": 102.}]
            sources[(symbol, day)] = {"bars": bars, "sha256": symbol + day}
    return sources, {**frozen, "sessions": sessions, "evaluation_sessions": sessions[-1:]}


def quote(ns, bid=100.58, ask=100.60, size=10):
    return {"t": orb.stamp(ns), "bp": bid, "ap": ask, "bs": size, "as": size,
            "bx": "Q", "ax": "Q", "c": ["R"]}


def indexed_quote(symbol, at, row):
    request = orb.request_window(symbol, at)
    return {(symbol, request["start"], request["end"]): {
        "rows": [row], "has_next_page": False, "raw_sha256": symbol + str(at)}}


def test_freeze_has_prior_20_and_disjoint_120_60_60_splits():
    frozen = orb.plan()
    assert len(frozen["sessions"]) == 260 and len(frozen["evaluation_sessions"]) == 240
    assert list(map(len, frozen["splits"].values())) == [120, 60, 60]
    assert len({day for days in frozen["splits"].values() for day in days}) == 240
    assert not any(frozen["source_attestations"].values()) and not frozen["live_order_eligible"]


def test_shortlist_uses_prior_turnover_and_only_first_five_minutes(panel):
    sources, frozen = panel
    day = frozen["evaluation_sessions"][0]
    candidates = orb.opening_candidates(sources, frozen["sessions"], day, "rvol")
    assert [r["symbol"] for r in candidates] == ["AAPL", "AMZN"]
    assert [r["symbol"] for r in orb.opening_candidates(sources, frozen["sessions"], day, "turnover")] == ["XOM", "NVDA"]
    for symbol in orb.SYMBOLS:
        sources[(symbol, day)]["bars"][-1].update(h=1001, c=1000, v=1000000000, vw=1000)
    assert orb.opening_candidates(sources, frozen["sessions"], day, "rvol") == candidates


def test_red_shortlist_slot_not_refilled_and_entry_uses_completed_breakout(panel):
    sources, frozen = panel
    day = frozen["evaluation_sessions"][0]
    first = orb.opening_candidates(sources, frozen["sessions"], day, "rvol")[0]
    bars = sources[(first["symbol"], day)]["bars"]
    intent = orb.entry_intent(first, bars)
    assert intent["ready"] == first["opening"] + 6 * orb.MINUTE + orb.SECOND
    assert intent["initial_stop"] == first["low"] and intent["limit"] == 100.75
    assert orb.entry_intent({**first, "close": 99.9}, bars) is None
    assert len(orb.opening_candidates(sources, frozen["sessions"], day, "rvol")) == 2


def test_expired_and_partial_quote_never_become_an_early_fill(panel):
    sources, frozen = panel
    day = frozen["evaluation_sessions"][0]
    c = orb.opening_candidates(sources, frozen["sessions"], day, "rvol")[0]
    intent = orb.entry_intent(c, sources[(c["symbol"], day)]["bars"])
    at = intent["ready"]
    index = indexed_quote(c["symbol"], at, quote(at + 5 * orb.SECOND))
    assert orb.quote_at(index, c["symbol"], at) == (None, None)
    next(iter(index.values()))["has_next_page"] = True
    assert orb.quote_at(index, c["symbol"], at)[1] == orb.request_window(c["symbol"], at)
    index = indexed_quote(c["symbol"], at, quote(at - orb.SECOND))
    assert orb.quote_at(index, c["symbol"], at) == (None, None)


def test_first_fill_nanoseconds_never_shorten_sixty_minute_deadline():
    opening, closing = orb.session_clocks("2024-01-02")
    filled = opening + 6 * orb.MINUTE + 1_900_000_000
    intent = {"initial_stop": 99, "closing": closing}
    exit_plan = orb.exit_clock(intent, {"ap": 100, "ns": filled}, [])
    assert exit_plan["deadline_ns"] == filled + 3600 * orb.SECOND
    assert exit_plan["at"] >= exit_plan["deadline_ns"]
    assert exit_plan["query_round_up_ns"] == 100000000


def test_intrabar_both_boundaries_choose_stop_with_ambiguity_flag():
    opening, closing = orb.session_clocks("2024-01-02")
    filled = opening + 6 * orb.MINUTE + orb.SECOND
    exit_plan = orb.exit_clock({"initial_stop": 99, "closing": closing}, {"ap": 100, "ns": filled},
                              [{"t": opening + 7 * orb.MINUTE, "l": 98, "h": 103}])
    assert exit_plan["reason"] == "stop_bar_close_reconstruction"
    assert exit_plan["ambiguity"] == ["intrabar_stop_order_unknown"]


def test_missing_entry_and_liquidation_make_account_incomplete_not_cash(panel):
    sources, frozen = panel
    missing = orb.replay(sources, {}, frozen, "rvol")
    assert not missing["complete"] and missing["net_return_pct"] is None and len(missing["needs"]) == 2
    quotes = {}
    day = frozen["evaluation_sessions"][0]
    for c in orb.opening_candidates(sources, frozen["sessions"], day, "rvol"):
        intent = orb.entry_intent(c, sources[(c["symbol"], day)]["bars"])
        quotes.update(indexed_quote(c["symbol"], intent["ready"], quote(intent["ready"])))
    result = orb.replay(sources, quotes, frozen, "rvol")
    assert not result["complete"] and result["net_return_pct"] is None
    assert len(result["needs"]) == 2
    for key in list(quotes):
        exit_at = orb.clock(key[1]) + orb.MINUTE
        request = orb.request_window(key[0], exit_at)
        quotes[(key[0], request["start"], request["end"])] = {"rows": [], "has_next_page": False}
    assert orb.replay(sources, quotes, frozen, "rvol")["net_return_pct"] is None


def test_closed_account_books_ask_bid_fees_and_risk_budget(panel):
    sources, frozen = panel
    quotes = {}
    day = frozen["evaluation_sessions"][0]
    for c in orb.opening_candidates(sources, frozen["sessions"], day, "rvol"):
        intent = orb.entry_intent(c, sources[(c["symbol"], day)]["bars"])
        entry = quote(intent["ready"])
        quotes.update(indexed_quote(c["symbol"], intent["ready"], entry))
        exit_at = orb.exit_clock(intent, {**entry, "ns": intent["ready"]}, sources[(c["symbol"], day)]["bars"])["at"]
        quotes.update(indexed_quote(c["symbol"], exit_at, quote(exit_at, 102.9, 102.92)))
    result = orb.replay(sources, quotes, frozen, "rvol")
    assert result["complete"] and len(result["trades"]) == 2
    for trade in result["trades"]:
        gross = trade["quantity"] * (trade["exit_bid"] - trade["entry_ask"])
        assert trade["pnl"] == pytest.approx(gross - trade["fees"])
        assert trade["planned_stop_loss"] <= 25 and trade["entry_notional"] < 2000
    assert result["net_return_pct"] == pytest.approx(sum(t["pnl"] for t in result["trades"]) / 100)
    assert result["days"][0]["time_weighted_entry_notional_pct"] > 0


def test_quote_loader_rejects_forged_complete_flag(tmp_path):
    at = orb.session_clocks("2024-01-02")[0] + 6 * orb.MINUTE
    request = orb.request_window("AAPL", at)
    body = {"symbol": "AAPL", "quotes": [], "next_page_token": "unseen"}
    raw = orb.json_bytes(body)
    envelope = {"schema": "alpaca-historical-quote-window-v1", "symbol": "AAPL", "status": "empty",
                "request": alpaca_research.parameters(**request), "raw_b64": base64.b64encode(raw).decode(),
                "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "quote_count": 0, "has_next_page": False}
    path = tmp_path / "quotes" / "AAPL" / "sample.json"
    orb.write_new(path, envelope)
    with pytest.raises(ValueError, match="pagination"):
        orb.load_quotes(tmp_path)


def test_quote_needs_plan_collects_both_rankings_and_delay_before_pnl(panel):
    sources, frozen = panel
    result = orb.quote_plan(sources, {}, frozen)
    assert len(result["windows"]) == 8
    assert {r["symbol"] for r in result["windows"]} == {"AAPL", "AMZN", "NVDA", "XOM"}
    assert not result["live_order_eligible"]


def test_paired_lower_uses_sessions_and_frozen_blocks():
    assert orb.paired_block_lower([1.] * 60) == 1
    assert orb.paired_block_lower([-1.] * 60) == -1
    assert orb.paired_block_lower([1.] * 59) is None


def test_positive_reconstruction_cannot_override_missing_source_attestations(monkeypatch):
    frozen = orb.plan()

    def synthetic_path(sources, quotes, plan, ranking, **kwargs):
        value = .01 if ranking == "rvol" else -.01
        return {"complete": True, "days": [
            {"day": day, "return_pct": value, "trade_count": 2,
             "time_weighted_entry_notional_pct": 1}
            for day in plan["evaluation_sessions"]],
            "trades": [{"day": day, "symbol": symbol, "pnl": 1, "ambiguity": []}
                       for day in plan["evaluation_sessions"] for symbol in ("AAPL", "MSFT")]}

    monkeypatch.setattr(orb, "replay", synthetic_path)
    result = orb.compare({}, {}, frozen)
    assert all(result["sealed_checks"].values())
    assert result["conditional_reconstruction_verdict"] == "passed_frozen_checks"
    assert result["verdict"] == "not_adopted_source_incomplete"
    assert not result["live_order_eligible"] and len(result["source_blockers"]) == 4


def test_bar_source_requires_full_session_and_exact_bytes(tmp_path):
    opening, closing = orb.session_clocks("2024-01-02")
    rows = [{"t": orb.stamp(t), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10, "vw": 100}
            for t in range(opening, closing, orb.MINUTE)]
    request = alpaca_research.bar_parameters("AAPL", orb.stamp(opening), orb.stamp(closing))
    def envelope(bars):
        raw = orb.json_bytes({"symbol": "AAPL", "bars": bars, "next_page_token": None})
        return {"schema": "alpaca-historical-bar-window-v1", "symbol": "AAPL", "status": "observed",
                "request": request, "window": {"start": orb.stamp(opening), "end": orb.stamp(closing)},
                "has_next_page": False, "raw_bytes": len(raw),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_b64": base64.b64encode(raw).decode()}
    path = tmp_path / "bars" / "AAPL" / "sample.json"
    orb.write_new(path, envelope(rows))
    assert len(orb.load_bars(tmp_path)[("AAPL", "2024-01-02")]["bars"]) == 390
    # This is a synthetic fixture, replaced deliberately to demonstrate the gap guard.
    path.write_bytes(orb.json_bytes(envelope(rows[:10] + rows[11:])))
    with pytest.raises(ValueError, match="missing minute"):
        orb.load_bars(tmp_path)
