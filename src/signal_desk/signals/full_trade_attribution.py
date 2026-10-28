"""Read-only attribution of the complete reference-paper trade journal.

The paper account uses moving-average cost, not FIFO. Old trades lack booked
cash/fees, so their fill-price result is a diagnostic proxy, never net P&L.
Nothing here changes a signal, exit, position, or order.
"""

from __future__ import annotations

from collections import defaultdict
import math

VERSION = "full-paper-journal-v1"


def _number(value: object, *, positive: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def _group(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(row[key])].append(row)
    result = []
    for label, members in buckets.items():
        result.append({"key": label, "sell_rows": len(members),
                       "loss_rows": sum(row["price_only_pnl"] < 0 for row in members),
                       "price_only_pnl": round(sum(row["price_only_pnl"] for row in members), 2),
                       "booked_net_pnl": round(sum(row["booked_net_pnl"] or 0 for row in members), 2),
                       "booked_net_rows": sum(row["booked_net_pnl"] is not None for row in members),
                       "cost_unknown_rows": sum(row["booked_net_pnl"] is None for row in members)})
    return sorted(result, key=lambda item: (item["price_only_pnl"], item["key"]))


def analyze(trades: list[dict], *, seed_cash: float, account: dict,
            trade_evidence: dict | None = None) -> dict:
    """Rebuild each sell at paper's moving-average basis and compare the book.

    `cash_proxy_pnl` uses observed cash when present and fill price otherwise.
    It is only a reconciliation diagnostic, not a historical net-return claim.
    """
    positions: dict[str, dict] = {}
    rows: list[dict] = []
    gaps: dict[str, int] = defaultdict(int)
    blocked: set[str] = set()
    recorded_cash_rows = 0
    from signal_desk.signals.execution_price_provenance import EVIDENCE_ORDER
    for trade in trades:
        ticker = str(trade.get("ticker") or "")
        side = str(trade.get("side") or "").lower()
        qty = _number(trade.get("qty"), positive=True)
        price = _number(trade.get("price"), positive=True)
        if not ticker or side not in {"buy", "sell"} or qty is None or qty != int(qty) or price is None:
            gaps["invalid_trade_rows"] += 1
            if ticker:
                blocked.add(ticker)
            continue
        if ticker in blocked:
            gaps["blocked_trade_rows"] += 1
            continue
        quantity = int(qty)
        evidence = (trade_evidence or {}).get(trade.get("id"), "unverifiable")
        cash = _number(trade.get("cash_change"))
        cash_valid = cash is not None and (cash < 0 if side == "buy" else cash > 0)
        if cash_valid:
            recorded_cash_rows += 1
        else:
            gaps["missing_or_invalid_cash_rows"] += 1
        if side == "buy":
            pos = positions.setdefault(ticker, {"qty": 0, "price_basis": 0.0,
                                                "cash_basis": 0.0, "basis_complete": True,
                                                "had_add": False, "price_evidence": evidence})
            pos["price_evidence"] = max(pos["price_evidence"], evidence, key=EVIDENCE_ORDER.get)
            pos["had_add"] = pos["had_add"] or pos["qty"] > 0 or trade.get("reason") == "ADD"
            pos["price_basis"] += price * quantity
            pos["cash_basis"] += -cash if cash_valid else price * quantity
            pos["basis_complete"] = pos["basis_complete"] and cash_valid
            pos["qty"] += quantity
            continue
        pos = positions.get(ticker)
        if not pos or pos["qty"] < quantity:
            gaps["unmatched_sell_rows"] += 1
            gaps["unmatched_sell_qty"] += quantity - (pos["qty"] if pos else 0)
            blocked.add(ticker)
            continue
        fraction = quantity / pos["qty"]
        # A fresh exit cannot sanitize a position opened/added with unknown prices.
        pos["price_evidence"] = max(pos["price_evidence"], evidence, key=EVIDENCE_ORDER.get)
        price_basis = pos["price_basis"] * fraction
        cash_basis = pos["cash_basis"] * fraction
        proceeds = cash if cash_valid else price * quantity
        exact = pos["basis_complete"] and cash_valid
        rows.append({"trade_id": trade.get("id"), "ticker": ticker,
                     "price_evidence": pos["price_evidence"],
                     "exit_reason": trade.get("reason") or "사유 미기록",
                     "entry_pattern": "추가매수 포함" if pos["had_add"] else "첫 매수만",
                     "qty": quantity,
                     "price_only_pnl": round(price * quantity - price_basis, 8),
                     "booked_net_pnl": round(proceeds - cash_basis, 8) if exact else None,
                     "cash_proxy_pnl": round(proceeds - cash_basis, 8)})
        pos["price_basis"] -= price_basis
        pos["cash_basis"] -= cash_basis
        pos["qty"] -= quantity
        if pos["qty"] == 0:
            del positions[ticker]

    book_positions = account.get("positions") or {}
    book_qty = {str(ticker): int(value["qty"]) for ticker, value in book_positions.items()}
    rebuilt_qty = {ticker: pos["qty"] for ticker, pos in positions.items() if pos["qty"]}
    mismatches = [{"ticker": ticker, "journal_qty": rebuilt_qty.get(ticker, 0),
                   "account_qty": book_qty.get(ticker, 0)}
                  for ticker in sorted(set(book_qty) | set(rebuilt_qty))
                  if book_qty.get(ticker, 0) != rebuilt_qty.get(ticker, 0)]
    cash = _number(account.get("cash"))
    invested = sum(float(pos["qty"]) * float(pos["avg_price"])
                   for pos in book_positions.values())
    account_realized = cash + invested - seed_cash if cash is not None else None
    proxy_realized = sum(row["cash_proxy_pnl"] for row in rows)
    delta = account_realized - proxy_realized if account_realized is not None else None
    complete = (not mismatches and not blocked and not gaps.get("invalid_trade_rows")
                and not gaps.get("unmatched_sell_rows") and delta is not None
                and abs(delta) <= (0.02 if seed_cash < 100_000 else 1.0))
    return {"version": VERSION, "mode": "shadow", "live_eligible": False,
            "trade_rows": len(trades), "recorded_cash_rows": recorded_cash_rows,
            "closed_sell_rows": len(rows), "open_positions": len(positions),
            "coverage_gaps": dict(gaps), "blocked_tickers": sorted(blocked),
            "position_mismatches": mismatches, "account_reconciled": complete,
            "account_realized_pnl": round(account_realized, 2) if account_realized is not None else None,
            "cash_proxy_realized_pnl": round(proxy_realized, 2),
            "account_minus_proxy": round(delta, 2) if delta is not None else None,
            "price_only_pnl": round(sum(row["price_only_pnl"] for row in rows), 2),
            "booked_net_pnl": round(sum(row["booked_net_pnl"] or 0 for row in rows), 2),
            "booked_net_sell_rows": sum(row["booked_net_pnl"] is not None for row in rows),
            "by_exit_reason": _group(rows, "exit_reason"),
            "by_entry_pattern": _group(rows, "entry_pattern"),
            "by_ticker": _group(rows, "ticker"),
            "by_price_evidence": _group(rows, "price_evidence"),
            "note": "가격 기준 손익은 과거 비용 누락분을 포함하지 않습니다. 추가매수·손절과 손익의 연관은 인과관계가 아닙니다."}
