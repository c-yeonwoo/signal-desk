"""Paper fill quote evidence is reported, not mistaken for a trading verdict."""

from signal_desk.signals import execution_price_provenance as study


def _event(kind, proof=None, *, ts=1000, reference=100):
    payload = {"reference_price": reference}
    if proof is not None:
        payload["price_evidence"] = proof
    return {"ticker": "ABC", "event_type": kind, "ts": ts, "payload": payload}


def test_summary_keeps_full_denominator_and_legacy_evidence_gaps():
    events = [_event("filled_buy"),
              _event("filled_sell", {"price_basis": "daily_close", "price_session": "2026-07-13"}),
              _event("filled_buy", {"price_basis": "intraday_provisional",
                                    "price_received_at": 990, "price_source_time_verified": False}),
              _event("planned_buy")]
    out = study.summarize(events, filled_events_total=8)
    assert out["sampled_recent_fills"] == 3
    assert out["unsampled_fills"] == 5
    assert out["missing_evidence"] == 1
    assert out["daily_close_reference"] == 1
    assert out["intraday_reference"] == 1
    assert out["intraday_source_time_unverified"] == 1
    assert out["state"] == "evidence_gaps"
    assert out["price_basis_verified"] is False
    assert out["live_eligible"] is False
    assert out["examples"][0]["status"] == "missing_evidence"


def test_invalid_or_late_quote_receipt_is_not_counted_as_verified():
    events = [_event("filled_buy", {"price_basis": "intraday_provisional",
                                    "price_received_at": 100, "price_source_time_verified": True}),
              _event("filled_sell", {"price_basis": "intraday_provisional",
                                     "price_source_time_verified": False})]
    out = study.summarize(events, filled_events_total=2)
    assert out["receipt_timing_anomalies"] == 1
    assert out["missing_received_at"] == 1
    assert out["intraday_source_time_marked_verified"] == 1
    assert out["state"] == "evidence_gaps"


def test_no_fills_does_not_look_healthy():
    out = study.summarize([], filled_events_total=0)
    assert out["state"] == "no_fills"
    assert out["sampled_recent_fills"] == 0


def test_trade_join_requires_exact_unique_order_and_source_evidence():
    from test_price_validity import NOW, proof
    trades = [{"id": i, "order_no": str(i), "ticker": "ABC", "side": "buy"} for i in range(1, 7)]
    def event(i, evidence):
        return {**_event("filled_buy", evidence, ts=NOW.timestamp()),
                "event_key": f"trade:kr:7:{i}"}
    events = [event(1, proof()), event(2, {**proof(), "price_source_timestamp": None}),
              event(3, {"price_basis": "daily_close"}), event(4, "malformed"),
              event(5, proof()), event(5, proof()), event(6, proof())]
    events[-1]["ticker"] = "OTHER"
    labels = study.classify_trades(trades, events, market="kr", uid=7)
    assert labels == {1: "observed_uncertified", 2: "unverifiable", 3: "invalid_or_delayed",
                      4: "unverifiable", 5: "unverifiable", 6: "unverifiable"}


def test_missing_receipt_or_unreadable_source_is_unknown_not_proven_stale():
    from test_price_validity import NOW, proof
    for field in ("price_received_at", "price_observation_id", "price_session", "price_source_timestamp"):
        evidence = {**proof(), field: None}
        event = {**_event("filled_buy", evidence, ts=NOW.timestamp()), "event_key": "trade:kr:7:1"}
        trades = [{"id": 1, "order_no": "1", "ticker": "ABC", "side": "buy"}]
        assert study.classify_trades(trades, [event], market="kr", uid=7) == {1: "unverifiable"}
    event["payload"]["price_evidence"]["price_source_timestamp"] = "not-a-timestamp"
    assert study.classify_trades(trades, [event], market="kr", uid=7) == {1: "unverifiable"}
