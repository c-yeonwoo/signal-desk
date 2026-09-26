"""토스 실계좌 직접 조회. 주문 전송이나 계좌번호 노출은 제공하지 않는다."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from signal_desk import config
from signal_desk.ingest import toss

_SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")


def _nonnegative(value: object) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid broker amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid broker amount")
    return str(amount)


def _selected_account() -> str:
    account = config.toss_account().strip()
    if not account.isascii() or not account.isdecimal() or int(account) <= 0:
        raise ValueError("invalid configured account sequence")
    return str(int(account))


def _verified_account() -> str | None:
    account = _selected_account()
    rows = toss.accounts()
    if rows is None:
        return None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid broker accounts")
        seq = row.get("accountSeq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError("invalid broker account sequence")
        if str(seq) == account and row.get("accountType") == "BROKERAGE":
            return account
    raise ValueError("configured account not found in brokerage accounts")


def snapshot() -> dict:
    """계좌 연결·현금 매수가능액·진행 중 주문을 조회한다. 응답은 시점별이며 원장이 아니다."""
    try:
        account = _verified_account()
    except ValueError:
        return {"ready": False, "reason": "설정된 토스 계좌가 계좌 목록과 일치하지 않습니다.",
                "order_transmission_enabled": False}
    if account is None:
        return {"ready": False, "reason": "토스 계좌 목록을 조회하지 못했습니다.",
                "order_transmission_enabled": False}
    krw = toss.buying_power(account, "KRW")
    usd = toss.buying_power(account, "USD")
    orders = toss.open_orders(account)
    if krw is None or usd is None or orders is None:
        return {"ready": False, "reason": "매수가능액 또는 진행 중 주문을 조회하지 못했습니다.",
                "account_verified": True, "order_transmission_enabled": False}
    try:
        if krw.get("currency") != "KRW" or usd.get("currency") != "USD":
            raise ValueError("broker currency mismatch")
        powers = {"KRW": _nonnegative(krw.get("cashBuyingPower")),
                  "USD": _nonnegative(usd.get("cashBuyingPower"))}
        raw_orders = orders.get("orders")
        if not isinstance(raw_orders, list):
            raise ValueError("invalid broker orders")
        clean_orders = []
        for row in raw_orders:
            if not isinstance(row, dict) or not _SYMBOL.fullmatch(str(row.get("symbol", ""))):
                raise ValueError("invalid broker order")
            execution = row.get("execution")
            if not isinstance(execution, dict) or row.get("side") not in ("BUY", "SELL"):
                raise ValueError("invalid broker order")
            clean_orders.append({"symbol": row["symbol"], "side": row["side"],
                                 "status": str(row.get("status") or "UNKNOWN"),
                                 "currency": row.get("currency"),
                                 "quantity": _nonnegative(row.get("quantity")),
                                 "filled_quantity": _nonnegative(execution.get("filledQuantity"))})
    except ValueError:
        return {"ready": False, "reason": "토스 계좌 응답 형식을 검증하지 못했습니다.",
                "account_verified": True, "order_transmission_enabled": False}
    return {"ready": True, "account_verified": True, "broker": "toss",
            "buying_power": powers, "open_order_count": len(clean_orders),
            "open_orders": clean_orders, "order_transmission_enabled": False,
            "note": "현금 기반 매수가능액과 진행 중 주문의 조회 시점 값입니다. 현금잔고·총자산·주문 승인과 다릅니다."}


def sellable(symbol: str) -> dict:
    symbol = str(symbol or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("invalid symbol")
    account = _verified_account()
    if account is None:
        return {"ready": False, "reason": "토스 계좌 목록을 조회하지 못했습니다."}
    result = toss.sellable_quantity(account, symbol)
    if result is None:
        return {"ready": False, "reason": "매도가능수량을 조회하지 못했습니다."}
    return {"ready": True, "symbol": symbol,
            "sellable_quantity": _nonnegative(result.get("sellableQuantity")),
            "order_transmission_enabled": False}
