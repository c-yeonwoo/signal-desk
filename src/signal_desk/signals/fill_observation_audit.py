"""Exact quote-ID comparison for a recorded paper fill, without price/time guessing."""

from __future__ import annotations

import datetime as dt
import math

from signal_desk.signals import price_validity


def audit(event: dict, quotes: list[dict], *, market: str, ledger_reference_price) -> dict:
    payload = event.get("payload") or {}
    proof = payload.get("price_evidence")
    result = {"state": "missing_evidence", "match": None,
              "source_time_verified": False, "profitability_verified": False}
    if (not isinstance(proof, dict) or not proof.get("price_observation_id")
            or not proof.get("price_session")):
        return result
    if proof.get("price_basis") != "intraday_provisional":
        return {**result, "state": "not_intraday"}
    try:
        reference = float(payload["reference_price"])
        ledger_reference = float(ledger_reference_price)
        at = dt.datetime.fromtimestamp(float(event["ts"]), dt.timezone.utc)
        received = float(proof["price_received_at"])
        if not all(math.isfinite(v) and v > 0 for v in (reference, received, ledger_reference)):
            raise ValueError("invalid reference")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return {**result, "state": "incomplete_observation"}
    # Timing is independently knowable even if the quote ledger was pruned or
    # the receipt age is outside the DB lookup's bounded window.
    validity = price_validity.check(market, reference, proof, now=at, require_history=False)
    result["price_validity_reason"] = validity["reason"]
    if not validity["eligible"]:
        if validity["reason"] == "source_time_missing":
            return {**result, "state": "source_metadata_incomplete"}
        if validity.get("quote_check", {}).get("source_time_state") == "unparseable":
            return {**result, "state": "source_time_unparseable"}
        return {**result, "state": "price_invalid_at_fill", "match": False}
    matched = [q for q in quotes if q.get("observation_id") == proof["price_observation_id"]]
    if not matched:
        return {**result, "state": "observation_not_retained"}
    if len(matched) != 1:
        return {**result, "state": "ambiguous_observation"}
    q = matched[0]
    try:
        observed = float(q["price"])
        if not math.isfinite(observed) or observed <= 0:
            raise ValueError("invalid observed price")
        # Quote-ledger ts is recording time; never pretend it is an exact receipt.
        if int(q["ts"]) > int(event["ts"]) or int(q["ts"]) < math.floor(received):
            return {**result, "state": "observation_recording_time_mismatch", "match": False}
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return {**result, "state": "incomplete_observation"}
    delta = reference - observed
    result.update(observation_id=proof["price_observation_id"], reference_price=reference,
                  observed_price=observed, reference_minus_observed=delta)
    if abs(reference - ledger_reference) > max(abs(reference) * 1e-9, 1e-8):
        return {**result, "state": "ledger_reference_mismatch", "match": False}
    if abs(delta) > max(abs(observed) * 1e-9, 1e-8):
        return {**result, "state": "reference_price_mismatch", "match": False}
    if not all((proof.get("price_source_timestamp"), q.get("source_timestamp"),
                proof.get("price_provider"), q.get("provider"))):
        return {**result, "state": "source_metadata_incomplete"}
    if (proof.get("price_source_timestamp") != q.get("source_timestamp")
            or proof.get("price_provider") != q.get("provider")
            or bool(proof.get("price_source_time_verified")) != bool(q.get("source_time_verified"))):
        return {**result, "state": "source_metadata_mismatch", "match": False}
    result["source_time_verified"] = validity["source_time_verified"]
    return {**result, "state": "same_observation_reference", "match": True}
