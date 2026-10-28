import datetime as dt

import pytest

from signal_desk.signals import price_validity


NOW = dt.datetime.fromisoformat("2026-10-27T02:00:00+00:00")


def proof(now=NOW):
    return {"price_basis": "intraday_provisional", "price_session": "2026-10-27",
            "price_observation_id": "observed-1", "price_received_at": now.timestamp() - 5,
            "price_source_timestamp": (now - dt.timedelta(seconds=10)).isoformat(),
            "price_source_time_verified": False}


def test_observed_is_eligible_but_not_certified():
    out = price_validity.check("kr", 100, proof(), close_date="2026-10-26", now=NOW)
    assert out["eligible"] and not out["source_time_verified"]
    assert out["valid_until"] == NOW.timestamp() + 590


@pytest.mark.parametrize("field,value,reason", [
    ("price_received_at", NOW.timestamp() - 601, "deferred_stale_quote"),
    ("price_received_at", NOW.timestamp() + 301, "deferred_stale_quote"),
    ("price_source_timestamp", "2026-10-27T01:49:59+00:00", "deferred_source_time"),
    ("price_source_timestamp", "2026-10-26T02:00:00+00:00", "deferred_source_time"),
    ("price_source_timestamp", "2026-10-27T11:00:00", "deferred_source_time"),
    ("price_source_timestamp", None, "source_time_missing"),
    ("price_observation_id", None, "deferred_stale_quote"),
])
def test_invalid_quote_defers_even_if_server_received_it_now(field, value, reason):
    evidence = {**proof(), field: value}
    out = price_validity.check("kr", 100, evidence, close_date="2026-10-26", now=NOW)
    assert not out["eligible"] and out["reason"] == reason


def test_fresh_quote_with_missing_close_defers_signal_not_price_only_exit():
    assert not price_validity.check("kr", 100, proof(), close_date="2026-10-23", now=NOW)["eligible"]
    assert price_validity.check("kr", 100, proof(), require_history=False, now=NOW)["eligible"]


def test_same_day_premarket_is_not_regular_session_quote():
    now = dt.datetime.fromisoformat("2026-10-27T00:01:00+00:00")
    evidence = proof(now)
    evidence["price_source_timestamp"] = "2026-10-26T23:59:00+00:00"
    assert price_validity.check("kr", 100, evidence, now=now)["reason"] == "outside_regular_session"


def test_api_deferred_is_not_hold_or_buy_and_keeps_reference_score():
    from signal_desk import api
    row = {"ticker": "005930", "kind": "STRONG_BUY", "score": 2.5, "rank_eligible": True,
           "signal_price_basis": "intraday_quote", "signal_close_date": "2026-10-26",
           "signal_computed_at": NOW.isoformat(), "signal_input_price": 100,
           "signal_quote_received_at": NOW.timestamp() - 5,
           "signal_price_evidence": {**proof(), "price_source_timestamp": None}}
    api._annotate_signal_price_status([row], "kr", NOW)
    assert row["kind"] == "DEFERRED" and row["reference_kind"] == "STRONG_BUY"
    assert row["score"] == 2.5 and not row["rank_eligible"]
    assert row["current_decision_status"] == "deferred"


def test_closed_market_is_reference_not_current_trade():
    row = {"kind": "BUY", "signal_price_status": "current_close", "signal_input_price": 100,
           "signal_close_date": "2026-10-27"}
    price_validity.annotate(row, "kr", now=NOW.replace(hour=12))
    assert row["kind"] == "BUY" and not row["current_decision_eligible"]
    assert row["current_decision_status"] == "close_reference_only"


def test_stale_input_never_reaches_advisor_or_updates_held_peak(tmp_path, monkeypatch):
    from test_bot import _setup, _seed, _sig, UID
    from signal_desk import bot, db
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}], {"AAA": [100., 108.]},
           [_sig("AAA", "가", "BUY", 2.)])
    prices, dates, snapshot = bot.store.load_engine_price_bundle("kr")
    snapshot["quote_meta"]["AAA"]["source_timestamp"] = "2020-01-01T00:00:00Z"
    monkeypatch.setattr(bot.store, "load_engine_price_bundle", lambda market: (prices, dates, snapshot))
    monkeypatch.setattr(bot.advisor, "advise", lambda *a, **kw: pytest.fail("invalid input reached LLM"))
    _seed(10000., {"AAA": {"name": "가", "qty": 1, "avg_price": 100.}})
    db.bot_position_upsert(UID, "AAA", "가", 1, 100., 100., "2026-10-20")
    out = bot.run_once(UID)
    assert not out["buys"] and not out["sells"]
    assert not out["unchecked_holdings"][0]["risk_checked"]
    assert db.bot_position_get(UID, "AAA")["peak_price"] == 100.
