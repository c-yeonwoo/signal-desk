"""Read-only coverage of the quote evidence attached to paper fill events."""

from __future__ import annotations

import datetime as dt
import math

EVIDENCE_ORDER = {"source_time_marked_verified": 0, "observed_uncertified": 1,
                  "unverifiable": 2, "invalid_or_delayed": 3}


def classify_trades(trades: list[dict], events: list[dict], *, market: str, uid: int) -> dict:
    """Join exact order IDs; never guess an old event from a price/time match."""
    from signal_desk.signals import price_validity
    by_key = {}
    for event in events:
        by_key.setdefault(event.get("event_key"), []).append(event)
    labels = {}
    for trade in trades:
        label = "unverifiable"
        matched = by_key.get(f"trade:{market}:{uid}:{trade.get('order_no')}", [])
        if len(matched) == 1:
            event = matched[0]
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            evidence = payload.get("price_evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            if (event.get("ticker") == trade.get("ticker")
                    and event.get("event_type") == f"filled_{trade.get('side')}"
                    and evidence.get("price_basis") == "daily_close"):
                label = "invalid_or_delayed"
            elif (event.get("ticker") == trade.get("ticker")
                  and event.get("event_type") == f"filled_{trade.get('side')}"
                  and evidence.get("price_basis") == "intraday_provisional"):
                try:
                    at = dt.datetime.fromtimestamp(float(event["ts"]), dt.timezone.utc)
                    received = float(evidence.get("price_received_at"))
                    if (not math.isfinite(received) or received <= 0
                            or not evidence.get("price_observation_id") or not evidence.get("price_session")):
                        raise ValueError("receipt evidence incomplete")
                    check = price_validity.check(market, payload.get("reference_price"), evidence,
                                                 now=at, require_history=False)
                    if check["eligible"]:
                        label = ("source_time_marked_verified" if check["source_time_verified"]
                                 else "observed_uncertified")
                    elif (check["reason"] not in {"source_time_missing", "invalid_price"}
                          and check.get("quote_check", {}).get("source_time_state") != "unparseable"):
                        label = "invalid_or_delayed"
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
        labels[trade["id"]] = label
    return labels


def summarize(events: list[dict], *, filled_events_total: int) -> dict:
    """Classify retained fills without pretending that an old event was verified.

    The event stream can be truncated by the DB query's 10,000-row cap. Counts
    refer to the observed recent subset; `filled_events_total` is the separate
    all-time denominator, never silently reduced to the subset.
    """
    if filled_events_total < 0:
        raise ValueError("invalid filled event count")
    fills = [event for event in events if event.get("event_type") in {"filled_buy", "filled_sell"}]
    if len(fills) > filled_events_total:
        raise ValueError("sample exceeds filled event denominator")
    counts = {"missing_evidence": 0, "daily_close_reference": 0,
              "intraday_reference": 0, "unknown_basis": 0,
              "intraday_source_time_marked_verified": 0, "intraday_source_time_unverified": 0,
              "missing_reference_price": 0, "missing_received_at": 0,
              "receipt_timing_anomalies": 0}
    samples = []
    for event in fills:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        proof = payload.get("price_evidence")
        if not isinstance(proof, dict) or not proof:
            counts["missing_evidence"] += 1
            status = "missing_evidence"
        else:
            basis = proof.get("price_basis")
            if basis == "daily_close":
                counts["daily_close_reference"] += 1
                status = "daily_close_reference"
            elif basis == "intraday_provisional":
                counts["intraday_reference"] += 1
                if proof.get("price_source_time_verified") is True:
                    counts["intraday_source_time_marked_verified"] += 1
                else:
                    counts["intraday_source_time_unverified"] += 1
                status = "intraday_reference"
                try:
                    received = float(proof["price_received_at"])
                    if not math.isfinite(received) or received <= 0:
                        raise ValueError("invalid quote receipt")
                    age = float(event["ts"]) - received
                    if not math.isfinite(age) or not -300 <= age <= 600:
                        counts["receipt_timing_anomalies"] += 1
                except (KeyError, TypeError, ValueError, OverflowError):
                    counts["missing_received_at"] += 1
            else:
                counts["unknown_basis"] += 1
                status = "unknown_basis"
        try:
            reference = float(payload["reference_price"])
            if not math.isfinite(reference) or reference <= 0:
                raise ValueError("invalid reference")
        except (KeyError, TypeError, ValueError, OverflowError):
            counts["missing_reference_price"] += 1
        if status != "intraday_reference" and len(samples) < 10:
            try:
                event_time = dt.datetime.fromtimestamp(float(event["ts"]), dt.timezone.utc).isoformat()
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                event_time = None
            samples.append({"ticker": event.get("ticker"), "event_type": event.get("event_type"),
                            "event_time_utc": event_time, "status": status,
                            "price_session": proof.get("price_session") if isinstance(proof, dict) else None})
    return {"schema": "paper-fill-price-provenance-v1", "mode": "read_only_diagnostic",
            "live_eligible": False, "price_basis_verified": False,
            "filled_events_total": filled_events_total, "sampled_recent_fills": len(fills),
            "unsampled_fills": filled_events_total - len(fills),
            "state": "no_fills" if not filled_events_total else
                     "evidence_gaps" if (filled_events_total > len(fills)
                                         or counts["missing_evidence"] or counts["unknown_basis"]
                                         or counts["missing_reference_price"] or counts["missing_received_at"]
                                         or counts["receipt_timing_anomalies"])
                     else "classified_not_certified",
            **counts, "examples": samples}
