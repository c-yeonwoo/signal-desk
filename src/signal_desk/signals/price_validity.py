"""Current decisions require an observed quote, not merely a calculated score.

This is an input-validity boundary, not a factor or a profitability threshold.
Historical close-based research keeps its original engine and sampling rules.
"""

from __future__ import annotations

import datetime as dt
import math

from signal_desk import market_clock
from signal_desk.broker import paper


def check(market: str, price, evidence: dict | None, *, close_date: str | None = None,
          now: dt.datetime | None = None, require_history: bool = True) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    market = "kr" if market == "kospi" else market
    evidence = evidence if isinstance(evidence, dict) else {}
    expected = market_clock.latest_completed_session(market, now)
    result = {"version": 1, "eligible": False, "reason": "invalid_price",
              "expected_close_date": expected, "checked_at": now.isoformat(),
              "source_time_verified": evidence.get("price_source_time_verified") is True}
    try:
        if isinstance(price, bool) or not math.isfinite(float(price)) or float(price) <= 0:
            return result
    except (TypeError, ValueError, OverflowError):
        return result
    quote = paper.bot_order_quote_check(market, evidence, now=now.timestamp())
    result["quote_check"] = quote
    if not quote["eligible"]:
        return {**result, "reason": quote["status"]}
    # Legacy paper compatibility accepts receipt-only quotes. Current judgments
    # do not: a new HTTP response must not certify an old provider price.
    if quote.get("source_time_state") != "within_window":
        return {**result, "reason": "source_time_missing"}
    window = market_clock.regular_window(market, now)
    source_at = dt.datetime.fromisoformat(str(evidence["price_source_timestamp"]).strip().replace("Z", "+00:00"))
    if (not window or float(evidence["price_received_at"]) < window[0].timestamp()
            or source_at < window[0]):
        return {**result, "reason": "outside_regular_session"}
    if require_history and (not expected or close_date != expected):
        return {**result, "reason": "incomplete_close_history"}
    return {**result, "eligible": True, "reason": "observed_quote",
            "valid_until": min(float(evidence["price_received_at"]) + 600,
                               source_at.timestamp() + 600, window[1].timestamp())}


def annotate(item: dict, market: str, *, now: dt.datetime) -> None:
    """Expose an explicit deferred verdict without mutating the research score."""
    validity = check(market, item.get("signal_input_price"), item.get("signal_price_evidence"),
                     close_date=item.get("signal_close_date"), now=now)
    item["price_validity"] = validity
    item["current_decision_eligible"] = validity["eligible"]
    item["reference_kind"] = item.get("reference_kind", item.get("kind"))
    if validity["eligible"]:
        item["current_decision_status"] = "eligible"
    elif item.get("signal_price_status") == "current_close":
        item["current_decision_status"] = "close_reference_only"
    else:
        item["current_decision_status"] = "deferred"
        item["kind"] = "DEFERRED"
        item["rank_eligible"] = False
        item["decision_buy_blocked"] = True
        item["hold_tag"] = "가격 확인 필요"
