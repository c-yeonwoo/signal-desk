"""자체 모의계좌(paper) 브로커 — 유저별 격리. 수동 거래는 캐시 가격을 쓸 수 있으나 봇은 신선한 장중 가격으로만 체결·
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from signal_desk import config, db, market_clock, notify, store
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


def bot_order_quote_check(market: str, evidence: dict | None, *, now: float | None = None) -> dict:
    """모의 주문의 가격 증거 검사. 공급자 시각의 파싱 성공은 체결시각 인증이 아니다."""
    result = {"version": 1, "eligible": False, "status": "deferred_stale_quote",
              "source_time_state": "not_checked", "source_time_verified": False}
    if market not in ("kr", "us") or not isinstance(evidence, dict):
        return result
    try:
        at = time.time() if now is None else float(now)
        instant = datetime.fromtimestamp(at, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return result
    if not market_clock.is_open(market, instant):
        return {**result, "status": "deferred_market_closed"}
    zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
    session = datetime.fromtimestamp(at, zone).date().isoformat()
    try:
        received_at = float(evidence.get("price_received_at"))
    except (TypeError, ValueError):
        return result
    receipt_ready = (evidence.get("price_basis") == "intraday_provisional"
                     and evidence.get("price_session") == session
                     and bool(evidence.get("price_observation_id"))
                     and math.isfinite(received_at)
                     and -300 <= at - received_at <= 600)
    if not receipt_ready:
        return result
    result["receipt_age_seconds"] = round(at - received_at, 3)
    raw_source_time = evidence.get("price_source_timestamp")
    if raw_source_time is None and not evidence.get("price_source_timestamp_field"):
        # Existing paper-only compatibility. Missing source time never becomes verified.
        return {**result, "eligible": True, "status": "ready_receipt_only",
                "source_time_state": "missing"}
    try:
        source_dt = datetime.fromisoformat(str(raw_source_time).strip().replace("Z", "+00:00"))
        if source_dt.tzinfo is None or source_dt.utcoffset() is None:
            raise ValueError("source timezone missing")
        source_at = source_dt.timestamp()
        source_session = source_dt.astimezone(zone).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return {**result, "status": "deferred_source_time", "source_time_state": "unparseable"}
    # Check both order time and receipt time; a future provider clock at receipt
    # must not become trustworthy merely because the bot ran a few minutes later.
    if source_at - min(at, received_at) > 300:
        state = "future"
    elif source_session != session:
        state = "different_session"
    elif at - source_at > 600:
        state = "stale"
    else:
        state = "within_window"
    return {**result, "eligible": state == "within_window",
            "status": "ready_source_time_observed" if state == "within_window" else "deferred_source_time",
            "source_time_state": state,
            "source_age_seconds": round(at - source_at, 3),
            "source_time_verified": evidence.get("price_source_time_verified") is True}


def bot_order_quote_ready(market: str, evidence: dict | None, *, now: float | None = None) -> bool:
    return bot_order_quote_check(market, evidence, now=now)["eligible"]


def bot_order_hold_status(market: str, evidence: dict | None = None) -> str:
    return bot_order_quote_check(market, evidence or {})["status"]


def defer_bot_order(uid: int, ticker: str, side: str, *, market: str, reason: str,
                    price: float | None, evidence: dict | None, name: str = "",
                    decision_check: dict | None = None) -> None:
    """체결 없이 보류를 감사 기록과 재시도 가능한 Telegram 아웃박스에 남긴다."""
    checked_at = time.time()
    at = int(checked_at)
    zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
    session = datetime.fromtimestamp(at, zone).date().isoformat()
    check = bot_order_quote_check(market, evidence or {}, now=checked_at)
    hold_status = check["status"]
    if decision_check and not decision_check.get("eligible") and check["eligible"]:
        hold_status = "deferred_price_decision"
    market_open = hold_status != "deferred_market_closed"
    key = f"paper-price-hold:{hold_status}:{market}:{session}:{uid}:{side}:{ticker}"
    alert_key = f"paper-price-hold:{hold_status}:{market}:{session}:{side}:{ticker}"
    action = "매도" if side == "sell" else "매수"
    cause = ("장중 현재가를 확인하지 못해 종가로 대신 체결하지 않았습니다."
             if market_open else "정규장이 닫혀 가상 체결하지 않았습니다.")
    if hold_status == "deferred_source_time":
        source_reason = {"stale": "가격 원문의 시각이 10분 넘게 지났습니다",
                         "different_session": "가격 원문의 날짜가 오늘 거래일과 다릅니다",
                         "future": "가격 원문의 시각이 수신·주문 시각보다 앞서 있습니다",
                         "unparseable": "가격 원문의 시각이나 시간대를 읽을 수 없습니다"}
        cause = f"{source_reason[check['source_time_state']]}. 가상 체결을 보류했습니다."
    if hold_status == "deferred_price_decision":
        cause = "가격의 원문 시각·거래 세션 또는 직전 종가 이력을 확인하지 못해 판정을 보류했습니다."
    text = (f"⚠️ 가상 {action} 보류 · {name or ticker}({ticker}) · {market.upper()}\n"
            f"{cause} 사유: {reason}. "
            f"{'가격 수집 상태를 확인해 주세요. ' if market_open else '다음 정규장에 다시 확인해 주세요. '}"
            f"{'손절·위험 청산도 실행되지 않았습니다.' if side == 'sell' else '다음 실행에서 다시 판단합니다.'}")
    notify.enqueue(text, dedupe_key=alert_key, priority="critical" if side == "sell" else "high",
                   expires_at=at + 12 * 3600, now=at)
    link = db.telegram_link_get(uid)
    if link and link["chat_id"] not in config.telegram_chat_ids():
        notify.enqueue_user(uid, text, dedupe_key=key, market=market, alert_type="paper_fill",
                            priority="critical" if side == "sell" else "high",
                            expires_at=at + 12 * 3600, now=at)
    db.execution_event_add(
        key, uid=uid, market=market, ticker=ticker, event_type=hold_status,
        price=price, ts=at,
        payload={"side": side, "reason": reason, "price_evidence": evidence or {},
                 "paper_quote_check": check,
                 "price_decision_check": decision_check,
                 "detail": ("source_time_inconsistent" if hold_status == "deferred_source_time"
                            else "fresh_intraday_quote_required" if market_open else "market_session_closed")},
    )


def place_order(uid: int, ticker: str, side: str, qty: int, price: float | None = None,
                name: str = "", market: str = "kr", *, reason: str | None = None,
                note: str | None = None, score: float | None = None,
                event_payload: dict | None = None, risk_policy: dict | None = None,
                alert_style: str | None = None, policy_id: str | None = None,
                signal_policy_id: str | None = None,
                enforce_fresh_quote: bool = False) -> dict | None:
    """유저 계좌 가상 체결. 봇 주문이면 잔고·체결·감사 이벤트를 한 트랜잭션으로 기록."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        return None
    if enforce_fresh_quote:
        evidence = (event_payload or {}).get("price_evidence")
        check = bot_order_quote_check(market, evidence)
        if not check["eligible"]:
            defer_bot_order(uid, ticker, side, market=market, reason=reason or "BOT",
                            price=price, evidence=evidence, name=name)
            return None
        event_payload = {**(event_payload or {}), "paper_quote_check": check}
    reference_price = float(price) if price else current_price(ticker)
    if not reference_price or reference_price <= 0:
        return None
    fill = execution.calculate(reference_price, qty, side, market)
    seed = _seed(uid, market)
    position_name = name or (_name_map(market).get(ticker, ticker) if side == "buy" else ticker)
    series = (store.load_us_price_series() if market == "us" else store.load_price_series()) if risk_policy else {}
    marks = {t: float(px[-1]) for t, px in series.items() if px}
    deferred_after_lock = False

    def update(old):
        nonlocal deferred_after_lock, event_payload
        if enforce_fresh_quote:
            final_check = bot_order_quote_check(market, (event_payload or {}).get("price_evidence"))
            if not final_check["eligible"]:
                deferred_after_lock = True
                return None, None
            event_payload = {**(event_payload or {}), "paper_quote_check": final_check}
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

    result = db.kv_transform(_key(uid, market), update, on_commit=write_audit)
    if deferred_after_lock:
        # Alert persistence opens its own transaction, so it must follow rollback.
        defer_bot_order(uid, ticker, side, market=market, reason=reason or "BOT",
                        price=price, evidence=(event_payload or {}).get("price_evidence"), name=name)
    return result
