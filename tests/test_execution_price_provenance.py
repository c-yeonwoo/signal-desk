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
