"""Observed paper-fill cost attribution, separate from any strategy promotion.

Only closed FIFO lots with both frozen reference prices and booked fees are counted.
Missing pre-ledger fields are coverage gaps, never zero-cost fills. The 2x scenario
is an arithmetic sensitivity check, not an executable fill or an order-book model.
"""

from __future__ import annotations

from collections import defaultdict
import math

VERSION = "observed-fill-cost-v1"


def _fields(event: dict) -> tuple[int, float, float, float] | None:
    payload = event.get("payload") or {}
    try:
        qty = int(payload["qty"])
        reference = float(payload["reference_price"])
        fill = float(event["price"])
        fees = float(payload["fees"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if qty <= 0 or min(reference, fill) <= 0 or fees < 0:
        return None
    if not all(math.isfinite(v) for v in (reference, fill, fees)):
        return None
    return qty, reference, fill, fees


def analyze(events: list[dict]) -> dict:
    """Attribute realized gross-vs-net cash drag to each closed FIFO lot fragment."""
    lots: dict[str, list[dict]] = defaultdict(list)
    rows: list[dict] = []
    skipped = defaultdict(int)
    blocked_tickers: set[str] = set()
    for event in events:
        ticker = str(event.get("ticker") or "")
        kind = event.get("event_type")
        if not ticker or kind not in {"filled_buy", "filled_sell"}:
            continue
        if ticker in blocked_tickers:
            skipped["blocked_unknown_quantity"] += 1
            continue
        fields = _fields(event)
        if kind == "filled_buy":
            if fields is None:
                skipped["incomplete_buy"] += 1
                # Keep an opaque lot: following sales cannot leapfrog an unknown basis.
                raw_qty = (event.get("payload") or {}).get("qty")
                try:
                    qty = int(raw_qty)
                except (TypeError, ValueError, OverflowError):
                    qty = 0
                if qty > 0:
                    lots[ticker].append({"remaining": qty, "fields": None})
                else:
                    blocked_tickers.add(ticker)
                continue
            lots[ticker].append({"remaining": fields[0], "fields": fields,
                                 "event_key": event.get("event_key")})
            continue
        if fields is None:
            skipped["incomplete_sell"] += 1
            # A sale with unknown price/fee still changes position size. Poison any
            # surviving basis; if quantity itself is unknown no later pairing is safe.
            try:
                missing_qty = int((event.get("payload") or {}).get("qty"))
            except (TypeError, ValueError, OverflowError):
                missing_qty = 0
            if missing_qty <= 0:
                blocked_tickers.add(ticker)
                continue
            while missing_qty > 0 and lots[ticker]:
                lot = lots[ticker][0]
                used = min(missing_qty, lot["remaining"])
                lot["remaining"] -= used
                missing_qty -= used
                if lot["remaining"] == 0:
                    lots[ticker].pop(0)
            for lot in lots[ticker]:
                lot["fields"] = None
            continue
        sell_qty, sell_ref, sell_fill, sell_fees = fields
        remaining = sell_qty
        while remaining > 0:
            if not lots[ticker]:
                skipped["unmatched_sell_qty"] += remaining
                break
            lot = lots[ticker][0]
            used = min(remaining, lot["remaining"])
            if lot["fields"] is None:
                skipped["unknown_basis_qty"] += used
            else:
                buy_qty, buy_ref, buy_fill, buy_fees = lot["fields"]
                gross = (sell_ref - buy_ref) * used
                net = (sell_fill * used - sell_fees * used / sell_qty
                       - buy_fill * used - buy_fees * used / buy_qty)
                drag = gross - net
                basis = buy_ref * used
                rows.append({"ticker": ticker, "quantity": used,
                             "buy_event_key": lot.get("event_key"),
                             "sell_event_key": event.get("event_key"),
                             "gross_reference_pnl": round(gross, 8),
                             "net_booked_pnl": round(net, 8),
                             "observed_cost_drag": round(drag, 8),
                             "reference_buy_notional": round(basis, 8),
                             "two_x_cost_pnl": round(gross - 2 * max(0.0, drag), 8)})
            lot["remaining"] -= used
            remaining -= used
            if lot["remaining"] == 0:
                lots[ticker].pop(0)
    gross = sum(r["gross_reference_pnl"] for r in rows)
    net = sum(r["net_booked_pnl"] for r in rows)
    drag = gross - net
    stress = sum(r["two_x_cost_pnl"] for r in rows)
    basis = sum(r["reference_buy_notional"] for r in rows)
    return {"version": VERSION, "mode": "shadow", "live_eligible": False,
            "closed_fragments": len(rows), "open_lots": sum(len(v) for v in lots.values()),
            "blocked_tickers": sorted(blocked_tickers), "coverage_gaps": dict(skipped),
            "gross_reference_pnl": round(gross, 8),
            "net_booked_pnl": round(net, 8), "observed_cost_drag": round(drag, 8),
            "gross_reference_return_pct": round(gross / basis * 100, 4) if basis else None,
            "net_booked_return_pct": round(net / basis * 100, 4) if basis else None,
            "two_x_cost_return_pct": round(stress / basis * 100, 4) if basis else None,
            "note": "2배 비용은 양(+)의 관측 차액만 증폭한 민감도 계산입니다. 호가/시장충격·실주문 체결 가능성은 검증하지 않았습니다.",
            "items": rows[-100:]}
