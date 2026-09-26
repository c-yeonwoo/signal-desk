"""토스 실보유 관측 성과 — 보유주식 전용, 계좌 전체 수익률과 엄격히 분리."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from signal_desk import db


def _money(value: object, *, negative: bool = True) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid broker amount") from None
    if not amount.is_finite() or (not negative and amount < 0):
        raise ValueError("nonfinite broker amount")
    return amount


def capture_toss(uid: int, holdings: dict, *, observed_at: int | None = None) -> list[dict]:
    """현재 응답의 시장별 평가액/당일 손익만 기록. 입출금·현금·실현손익은 추측하지 않는다."""
    items = holdings.get("items")
    if not isinstance(items, list):
        raise ValueError("missing broker holdings")
    observed_at = int(observed_at if observed_at is not None else time.time())
    observed_date = datetime.fromtimestamp(observed_at, ZoneInfo("Asia/Seoul")).date().isoformat()
    groups = {"kr": [], "us": []}
    for item in items:
        if not isinstance(item, dict) or item.get("marketCountry") not in ("KR", "US"):
            raise ValueError("unknown broker market")
        groups[item["marketCountry"].lower()].append(item)
    out = []
    pending = []
    for market, rows in groups.items():
        currency = "KRW" if market == "kr" else "USD"
        value = Decimal(0)
        pnl = Decimal(0)
        complete_pnl = bool(rows)
        for item in rows:
            if item.get("currency") not in (None, currency):
                raise ValueError("broker currency mismatch")
            market_value = item.get("marketValue")
            daily_profit_loss = item.get("dailyProfitLoss")
            if not isinstance(market_value, dict) or (daily_profit_loss is not None
                                                      and not isinstance(daily_profit_loss, dict)):
                raise ValueError("invalid broker amount object")
            value += _money(market_value.get("amount"), negative=False)
            daily = daily_profit_loss.get("amount") if daily_profit_loss is not None else None
            if daily is None:
                complete_pnl = False
            else:
                pnl += _money(daily)
        prior = value - pnl
        rate = float(pnl / prior * 100) if complete_pnl and prior > 0 else None
        quality = ("holdings_empty" if not rows else "holdings_daily_provisional"
                   if complete_pnl else "holdings_valuation_only")
        payload = {"market": market, "date": observed_date, "value": str(value),
                   "daily_pnl": str(pnl) if complete_pnl else None, "count": len(rows),
                   "quality": quality, "bucket": observed_at // 900}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        pending.append(dict(uid=uid, broker="toss", market=market, observed_date=observed_date,
                            observed_at=observed_at, currency=currency, holdings_value=str(value),
                            daily_pnl=str(pnl) if complete_pnl else None, daily_return_pct=rate,
                            holdings_count=len(rows), quality=quality, digest=digest))
        out.append(payload)
    db.account_observations_add(pending)
    return out


def history(uid: int, market: str) -> dict:
    if market not in ("kr", "us"):
        raise ValueError("invalid market")
    points = db.account_observations_daily(uid, broker="toss", market=market)
    return {
        "market": market, "broker": "toss", "scope": "holdings_only",
        "account_return_available": False, "points": points,
        "note": "증권사 보유주식 조회값의 관측일별 변화입니다. 현금·입출금·매도 후 실현손익이 없어 계좌 전체 수익률이나 전략 성과가 아닙니다.",
    }
