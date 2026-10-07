"""자체 모의계좌(paper) 브로커 — 유저별 격리. 외부 연결 없이 가격캐시(종가) 기준으로 가상 체결·
현금·포지션을 유저마다 내부 관리한다. 시그널(판단)은 공용, 계좌·실행은 유저별.

계좌 상태는 db.kv('paper_account:{uid}')에 JSON(현금+포지션). 초기 현금은 유저별 시드(user_bot.seed_cash).
한계: 실시장 미시구조(호가·슬리피지·부분체결) 없음 — 신호가/종가로 즉시 전량 체결로 가정.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid

from signal_desk import db, store
from signal_desk.broker import execution

log = logging.getLogger("signal_desk.broker.paper")


def _key(uid: int, market: str = "kr") -> str:
    return f"paper_account:{uid}" if market == "kr" else f"paper_account:{uid}:{market}"


def _seed(uid: int, market: str = "kr") -> float:
    u = db.user_bot_get(uid) or {}
    return float((u.get("seed_cash_us") if market == "us" else u.get("seed_cash")) or (10_000 if market == "us" else 10_000_000))


def _load(uid: int, market: str = "kr") -> dict:
    raw = db.kv_get(_key(uid, market))
    if not raw:
        return {"cash": _seed(uid, market), "positions": {}}
    acct = json.loads(raw) if isinstance(raw, str) else raw
    acct.setdefault("cash", _seed(uid, market))
    acct.setdefault("positions", {})
    return acct


def _save(uid: int, acct: dict, market: str = "kr") -> None:
    db.kv_set(_key(uid, market), json.dumps(acct, ensure_ascii=False))


def _name_map(market: str = "kr") -> dict[str, str]:
    uni = store.load_us_universe() if market == "us" else store.load_universe()
    return {u["ticker"]: u["name"] for u in uni}


def current_price(ticker: str) -> float | None:
    """가격캐시(최근 종가) 기준 현재가 — 국내+해외 병합. 없으면 None."""
    closes = store.load_price_series().get(ticker) or store.load_us_price_series().get(ticker)
    return float(closes[-1]) if closes else None


def balance(uid: int, market: str = "kr") -> dict:
    """KIS balance와 동일 형태 — 유저 모의계좌(시장별) 현금·보유·평가손익. 항상 성공."""
    acct = _load(uid, market)
    names = _name_map(market)
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
                name: str = "", market: str = "kr", *, reason: str | None = None,
                note: str | None = None, score: float | None = None,
                event_payload: dict | None = None, risk_policy: dict | None = None,
                alert_style: str | None = None, policy_id: str | None = None,
                signal_policy_id: str | None = None) -> dict | None:
    """유저 계좌 가상 체결. 봇 주문이면 잔고·체결·감사 이벤트를 한 트랜잭션으로 기록."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        return None
    reference_price = float(price) if price else current_price(ticker)
    if not reference_price or reference_price <= 0:
        return None
    fill = execution.calculate(reference_price, qty, side, market)
    seed = _seed(uid, market)
    position_name = name or (_name_map(market).get(ticker, ticker) if side == "buy" else ticker)
    series = (store.load_us_price_series() if market == "us" else store.load_price_series()) if risk_policy else {}
    marks = {t: float(px[-1]) for t, px in series.items() if px}

    def update(old):
        acct = json.loads(old) if isinstance(old, str) else (old or {"cash": seed, "positions": {}})
        acct.setdefault("cash", seed)
        acct.setdefault("positions", {})
        pos = acct["positions"].get(ticker)
        if side == "buy":
            if -fill.cash_change > acct["cash"]:
                return None, None
            if risk_policy:
                if not pos and len(acct["positions"]) >= int(risk_policy["max_positions"]):
                    return None, None
                buy_mark = max(reference_price, marks.get(ticker, reference_price))
                stock_eval = sum(float(p["qty"]) *
                                 (buy_mark if t == ticker else marks.get(t, float(p["avg_price"])))
                                 for t, p in acct["positions"].items())
                current_value = float(pos["qty"]) * buy_mark if pos else 0.0
                post_stock = stock_eval + qty * buy_mark
                post_total = acct["cash"] + fill.cash_change + post_stock
                exposure = float(risk_policy["exposure"])
                position_pct = float(risk_policy["position_pct"])
                if not all(math.isfinite(v) and v >= 0 for v in (post_total, exposure, position_pct)):
                    return None, None
                if post_stock > post_total * exposure + 1e-8:
                    return None, None
                if current_value + qty * buy_mark > post_total * position_pct + 1e-8:
                    return None, None
            acct["cash"] += fill.cash_change
            if pos:
                total = pos["qty"] + qty
                pos["avg_price"] = (pos["avg_price"] * pos["qty"] - fill.cash_change) / total
                pos["qty"] = total
            else:
                acct["positions"][ticker] = {"name": position_name, "qty": qty,
                                             "avg_price": -fill.cash_change / qty}
        else:
            if not pos or pos["qty"] < qty:
                return None, None
            acct["cash"] += fill.cash_change
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                del acct["positions"][ticker]
        result = {"order_no": f"PAPER-{uuid.uuid4().hex[:16]}", "order_time": "", **fill.as_dict()}
        return json.dumps(acct, ensure_ascii=False), result

    def write_audit(c, result):
        if reason is None:
            return  # 직접 paper 사용은 봇 전략 체결로 분류하지 않는다.
        at = int(time.time())
        c.execute("INSERT INTO bot_trades(uid,ticker,market,name,side,qty,price,reason,order_no,ts,score,note,"
                  "reference_price,fees,slippage_cost,cash_change) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, ticker, market, position_name, side, qty, result["fill_price"], reason,
                   result["order_no"], at, score, note, reference_price, result["total_fees"],
                   result["slippage_cost"], result["cash_change"]))
        payload = {"qty": qty, "reason": reason, "reference_price": reference_price,
                   "fees": result["total_fees"], "slippage_cost": result["slippage_cost"],
                   **(event_payload or {})}
        # Prefer the exact price-reference evidence captured by the bot cycle. The latest
        # observation at commit time may differ from the price that sizing used.
        if "price_evidence" not in payload:
            payload["price_evidence"] = store.live_price_evidence(ticker)
        if policy_id:
            payload["execution_policy_id"] = policy_id
        if signal_policy_id:
            payload["signal_policy_id"] = signal_policy_id
        if score is not None:
            payload["score"] = score
        c.execute("INSERT INTO execution_events(event_key,uid,market,ticker,event_type,price,payload,ts) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (f"trade:{market}:{uid}:{result['order_no']}", uid, market, ticker,
                   f"filled_{side}", result["fill_price"], json.dumps(payload, ensure_ascii=False, sort_keys=True),
                   at))
        if alert_style:
            from signal_desk import bot_alerts
            if bot_alerts.selected(uid, {uid: alert_style}):
                row = {"side": side.upper(), "name": position_name, "ticker": ticker,
                       "qty": qty, "price": result["fill_price"], "reason": reason,
                       "score": score, "order_no": result["order_no"]}
                c.execute("INSERT OR IGNORE INTO notification_outbox("
                          "dedupe_key,text,priority,status,attempts,next_attempt,expires_at,created) "
                          "VALUES(?,?,'critical','pending',0,?,?,?)",
                          (bot_alerts.dedupe_key(uid, market, [row]),
                           bot_alerts.render(alert_style, market, [row]), at, at + 12 * 3600, at))

    return db.kv_transform(_key(uid, market), update, on_commit=write_audit)
