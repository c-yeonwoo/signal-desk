"""자체 모의계좌(paper) 브로커 — 유저별 격리. 외부 연결 없이 가격캐시(종가) 기준으로 가상 체결·
현금·포지션을 유저마다 내부 관리한다. 시그널(판단)은 공용, 계좌·실행은 유저별.

계좌 상태는 db.kv('paper_account:{uid}')에 JSON(현금+포지션). 초기 현금은 유저별 시드(user_bot.seed_cash).
한계: 실시장 미시구조(호가·슬리피지·부분체결) 없음 — 신호가/종가로 즉시 전량 체결로 가정.
"""

from __future__ import annotations

import json
import logging

from signal_desk import db, store

log = logging.getLogger("signal_desk.broker.paper")


def _key(uid: int) -> str:
    return f"paper_account:{uid}"


def _seed(uid: int) -> float:
    return float((db.user_bot_get(uid) or {}).get("seed_cash") or 10_000_000)


def _load(uid: int) -> dict:
    raw = db.kv_get(_key(uid))
    if not raw:
        return {"cash": _seed(uid), "positions": {}}
    acct = json.loads(raw) if isinstance(raw, str) else raw
    acct.setdefault("cash", _seed(uid))
    acct.setdefault("positions", {})
    return acct


def _save(uid: int, acct: dict) -> None:
    db.kv_set(_key(uid), json.dumps(acct, ensure_ascii=False))


def _name_map() -> dict[str, str]:
    return {u["ticker"]: u["name"] for u in store.load_universe()}


def current_price(ticker: str) -> float | None:
    """가격캐시(최근 종가) 기준 현재가 — 국내+해외 병합. 없으면 None."""
    closes = store.load_price_series().get(ticker) or store.load_us_price_series().get(ticker)
    return float(closes[-1]) if closes else None


def balance(uid: int) -> dict:
    """KIS balance와 동일 형태 — 유저 모의계좌 현금·보유·평가손익. 항상 성공."""
    acct = _load(uid)
    names = _name_map()
    holdings, stock_eval, invested = [], 0.0, 0.0
    for t, p in acct["positions"].items():
        price = current_price(t) or p["avg_price"]
        stock_eval += price * p["qty"]
        invested += p["avg_price"] * p["qty"]
        pnl_pct = round((price / p["avg_price"] - 1) * 100, 2) if p["avg_price"] else 0.0
        holdings.append({"ticker": t, "name": p.get("name") or names.get(t, t),
                         "qty": p["qty"], "avg_price": round(p["avg_price"], 2),
                         "price": round(price, 2), "pnl_pct": pnl_pct})
    pnl = stock_eval - invested
    return {
        "cash": round(acct["cash"], 2), "stock_eval": round(stock_eval, 2),
        "invested": round(invested, 2), "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / invested * 100, 2) if invested else None,
        "total_eval": round(acct["cash"] + stock_eval, 2), "holdings": holdings,
    }


def place_order(uid: int, ticker: str, side: str, qty: int, price: float | None = None,
                name: str = "") -> dict | None:
    """유저 계좌 가상 체결. 실패(현금·수량 부족, 가격 없음) 시 None. 성공 시 order 유사 dict."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        return None
    px = float(price) if price else current_price(ticker)
    if not px or px <= 0:
        return None
    acct = _load(uid)
    pos = acct["positions"].get(ticker)
    if side == "buy":
        if px * qty > acct["cash"]:
            return None
        acct["cash"] -= px * qty
        if pos:
            total = pos["qty"] + qty
            pos["avg_price"] = (pos["avg_price"] * pos["qty"] + px * qty) / total
            pos["qty"] = total
        else:
            acct["positions"][ticker] = {"name": name or _name_map().get(ticker, ticker),
                                         "qty": qty, "avg_price": px}
    else:  # sell
        if not pos or pos["qty"] < qty:
            return None
        acct["cash"] += px * qty
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del acct["positions"][ticker]
    _save(uid, acct)
    return {"order_no": "PAPER", "order_time": "", "fill_price": round(px, 2)}
