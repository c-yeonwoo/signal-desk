import copy
import json

from signal_desk import db
from signal_desk.signals import fill_observation_audit as audit
from test_price_validity import NOW, proof


def sample():
    evidence = {**proof(), "price_provider": "toss"}
    event = {"ts": int(NOW.timestamp()), "price": 99.95,
             "payload": {"reference_price": 100., "price_evidence": evidence}}
    quotes = [{"ts": int(evidence["price_received_at"]), "price": 100.,
               "observation_id": evidence["price_observation_id"], "provider": "toss",
               "source_timestamp": evidence["price_source_timestamp"]}]
    return event, quotes


def test_exact_id_reference_matches_despite_adverse_fill_slippage():
    event, quotes = sample()
    before = copy.deepcopy((event, quotes))
    out = audit.audit(event, quotes, market="kr", ledger_reference_price=100.)
    assert out["match"] is True and out["state"] == "same_observation_reference"
    assert out["reference_minus_observed"] == 0
    assert out["source_time_verified"] is False and out["profitability_verified"] is False
    assert (event, quotes) == before


def test_same_price_at_nearby_time_cannot_replace_missing_id():
    event, quotes = sample()
    quotes[0]["observation_id"] = "another-observation"
    out = audit.audit(event, quotes, market="kr", ledger_reference_price=100.)
    assert out["match"] is None and out["state"] == "observation_not_retained"


def test_price_metadata_and_ledger_reference_conflicts_are_not_matches():
    for field, value, state in (("price", 120., "reference_price_mismatch"),
                                ("provider", "other", "source_metadata_mismatch"),
                                ("ts", NOW.timestamp() + 1, "observation_recording_time_mismatch")):
        event, quotes = sample()
        quotes[0][field] = value
        out = audit.audit(event, quotes, market="kr", ledger_reference_price=100.)
        assert out["match"] is False and out["state"] == state
    event, quotes = sample()
    assert audit.audit(event, quotes, market="kr", ledger_reference_price=120.)["state"] == "ledger_reference_mismatch"
    assert audit.audit(event, quotes + quotes, market="kr", ledger_reference_price=100.)["match"] is None
    quotes[0]["source_timestamp"] = None
    assert audit.audit(event, quotes, market="kr", ledger_reference_price=100.)["match"] is None
    event, quotes = sample()
    event["payload"]["price_evidence"].pop("price_session")
    assert audit.audit(event, quotes, market="kr", ledger_reference_price=100.)["match"] is None
    event, quotes = sample()
    event["payload"]["price_evidence"]["price_source_time_verified"] = True
    assert audit.audit(event, quotes, market="kr", ledger_reference_price=100.)["state"] == "source_metadata_mismatch"


def test_fresh_receipt_with_old_source_does_not_pass():
    event, quotes = sample()
    source = "2026-10-27T01:40:00+00:00"
    event["payload"]["price_evidence"]["price_source_timestamp"] = source
    quotes[0]["source_timestamp"] = source
    out = audit.audit(event, quotes, market="kr", ledger_reference_price=100.)
    assert out["match"] is False and out["state"] == "price_invalid_at_fill"


def test_existing_case_reader_joins_only_its_market_ticker_and_observation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    event, quotes = sample()
    uid, ts = 900002, event["ts"]
    db.bot_trade_log(uid, "005930", "삼성전자", "sell", 1, 99.95, "STOP_LOSS", "PAPER-join",
                     market="kr", reference_price=100.)
    c = db.conn()
    c.execute("UPDATE bot_trades SET ts=?", (ts,))
    c.commit()
    c.close()
    db.execution_event_add(f"trade:kr:{uid}:PAPER-join", uid=uid, market="kr", ticker="005930",
                           event_type="filled_sell", price=99.95, ts=ts,
                           payload={**event["payload"], "qty": 1})
    for market, ticker, px in (("us", "005930", 999.), ("kr", "000660", 999.), ("kr", "005930", 100.)):
        db.intraday_quotes_record(market, {ticker: {**quotes[0], "price": px}}, ts=quotes[0]["ts"])
    row = db.bot_trades_for_case(uid, "kr", "005930", ts - 1, ts + 1)["trades"][0]
    assert row["execution_event_state"] == "matched"
    assert row["price_observation_audit"]["match"] is True
    stale_event, _ = sample()
    stale_event["payload"]["price_evidence"]["price_received_at"] = ts - 601
    # Change only this synthetic fixture; production observations stay immutable.
    c = db.conn()
    c.execute("UPDATE execution_events SET payload=? WHERE event_key=?",
              (json.dumps({**stale_event["payload"], "qty": 1}), f"trade:kr:{uid}:PAPER-join"))
    c.commit()
    c.close()
    row = db.bot_trades_for_case(uid, "kr", "005930", ts - 1, ts + 1)["trades"][0]
    assert row["price_observation_audit"]["state"] == "price_invalid_at_fill"
    assert row["price_observation_audit"]["match"] is False
    c = db.conn()
    c.execute("UPDATE execution_events SET uid=42 WHERE event_key=?", (f"trade:kr:{uid}:PAPER-join",))
    c.commit()
    c.close()
    row = db.bot_trades_for_case(uid, "kr", "005930", ts - 1, ts + 1)["trades"][0]
    assert row["execution_event_state"] == "mismatch"
    assert row["price_observation_audit"]["state"] == "execution_event_unverified"
    assert row["price_observation_audit"]["match"] is None
