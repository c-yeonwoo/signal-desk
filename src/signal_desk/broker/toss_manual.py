"""국내주식 소액 지정가의 사용자 개별 승인 파일럿. 자동 추종/시장가/US 주문은 없다."""

from __future__ import annotations

import datetime
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo

from signal_desk import bot, config, db
from signal_desk.broker import intent_ledger, live_policy, toss_order, toss_readonly
from signal_desk.ingest import toss
from signal_desk.signals import advisor_shadow

PILOT_MAX_ORDER_KRW = Decimal("100000")
PILOT_MAX_DAILY_BUY_KRW = Decimal("200000")
_KST = ZoneInfo("Asia/Seoul")


def availability() -> dict:
    """운영에서는 영속 SQLite 볼륨과 별도 명시적 스위치가 모두 필요하다."""
    blockers = []
    if os.environ.get("TOSS_MANUAL_ORDER_ENABLED") != "true":
        blockers.append("manual_order_switch_off")
    if config.is_prod():
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
        if mount != "/app/data" or not Path(mount).is_dir() or not db.DB.resolve().is_relative_to(Path(mount)):
            blockers.append("persistent_ledger_volume_unverified")
    return {"enabled": not blockers, "mode": "manual_kr_limit_pilot", "blockers": blockers,
            "max_order_krw": str(PILOT_MAX_ORDER_KRW),
            "max_daily_buy_krw": str(PILOT_MAX_DAILY_BUY_KRW),
            "automatic_copy_enabled": False}


def _require_enabled() -> None:
    if not availability()["enabled"]:
        raise ValueError("실주문 파일럿이 비활성화되어 있습니다. 영속 볼륨·운영 설정을 확인하세요.")


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid broker amount")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid broker amount") from None
    if not n.is_finite() or n < 0 or (positive and n == 0):
        raise ValueError("invalid broker amount")
    return n


def _source(style: str, event_id: int, now: int) -> dict:
    if (style not in ("conservative", "balanced", "aggressive") or isinstance(event_id, bool)
            or not isinstance(event_id, int) or event_id <= 0):
        raise ValueError("참조 봇 체결을 다시 선택하세요.")
    bot.ensure_reference_bots()
    ref_uid = next(uid for uid, name in bot.REFERENCE_BOTS.items() if name == style)
    event = db.bot_trade_get(ref_uid, event_id, "kr")
    if not event or event.get("side") not in ("buy", "sell") or not isinstance(event.get("ts"), int):
        raise ValueError("참조 체결을 확인할 수 없습니다.")
    if not 0 <= now - event["ts"] <= 15 * 60:
        raise ValueError("참조 체결이 15분을 넘어 주문 후보가 아닙니다.")
    if event["side"] == "buy":
        try:
            safety = advisor_shadow.decision_status(style=style, summary=advisor_shadow.cached_summary())
        except Exception:
            raise ValueError("참조 봇의 신규 매수 안전 상태를 확인하지 못했습니다.") from None
        if not safety.get("buy_path_active"):
            raise ValueError("참조 봇 신규 매수가 안전 게이트에서 보류되었습니다.")
    return event


def _regular_session(now: int) -> None:
    calendar = toss.kr_market_calendar()
    day = calendar.get("today") if isinstance(calendar, dict) else None
    today = datetime.datetime.fromtimestamp(now, _KST).date().isoformat()
    regular = ((day or {}).get("integrated") or {}).get("regularMarket") if isinstance(day, dict) else None
    if not isinstance(regular, dict) or day.get("date") != today:
        raise ValueError("국내 정규장 운영 시간을 확인하지 못했거나 휴장일입니다.")
    try:
        start = datetime.datetime.fromisoformat(regular["startTime"])
        cutoff = datetime.datetime.fromisoformat(regular["singlePriceAuctionStartTime"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("증권사 장 운영 시간 검증 실패") from None
    if start.tzinfo is None or cutoff.tzinfo is None or not start <= datetime.datetime.fromtimestamp(now, _KST) < cutoff:
        raise ValueError("국내 정규장 연속매매 시간에만 주문할 수 있습니다.")


def _quote(symbol: str, now: int) -> tuple[Decimal, int]:
    quote = toss.price_quote(symbol)
    if not isinstance(quote, dict) or quote.get("currency") != "KRW":
        raise ValueError("현재가와 통화를 확인하지 못했습니다.")
    try:
        stamp = datetime.datetime.fromisoformat(quote["timestamp"])
        ts = int(stamp.timestamp())
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("현재가 시각을 확인하지 못했습니다.") from None
    if stamp.tzinfo is None or not -3 <= now - ts <= 30:
        raise ValueError("현재가가 오래되어 다시 조회해야 합니다.")
    return _decimal(quote.get("lastPrice"), positive=True), ts


def _account_values(account: str, symbol: str, side: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    buying = toss.buying_power(account, "KRW")
    holdings = toss.holdings(account)
    orders = toss.open_orders(account)
    if (not isinstance(buying, dict) or buying.get("currency") != "KRW"
            or not isinstance(holdings, dict) or not isinstance(holdings.get("items"), list)
            or not isinstance(orders, dict) or not isinstance(orders.get("orders"), list)
            or orders.get("hasNext") not in (None, False)
            or orders.get("nextCursor") not in (None, "")):
        raise ValueError("계좌 여력·보유·미체결 주문을 완전하게 확인하지 못했습니다.")
    if orders["orders"]:
        raise ValueError("진행 중 주문이 있어 새 실주문을 보류합니다.")
    cash = _decimal(buying.get("cashBuyingPower"))
    total_holdings = Decimal(0)
    position = Decimal(0)
    for item in holdings["items"]:
        if not isinstance(item, dict) or item.get("marketCountry") not in ("KR", "US"):
            raise ValueError("보유 주식 시장을 확인하지 못했습니다.")
        if item["marketCountry"] != "KR":
            continue
        if item.get("currency") != "KRW" or not isinstance(item.get("marketValue"), dict):
            raise ValueError("국내 보유 주식 평가액 검증 실패")
        value = _decimal(item["marketValue"].get("amount"))
        total_holdings += value
        if item.get("symbol") == symbol:
            position += value
    sellable = Decimal(0)
    if side == "SELL":
        quantity = toss.sellable_quantity(account, symbol)
        if not isinstance(quantity, dict):
            raise ValueError("매도가능수량 조회 실패")
        sellable = _decimal(quantity.get("sellableQuantity"))
    return cash, total_holdings, position, sellable


def _inspect(uid: int, style: str, event_id: int, limit_price: int, now: int) -> dict:
    _require_enabled()
    if isinstance(limit_price, bool) or not isinstance(limit_price, int) or not 0 < limit_price <= 100_000_000:
        raise ValueError("양의 정수 지정가가 필요합니다.")
    policy = db.live_copy_policy_get(uid)
    if not policy["configured"] or policy["source_style"] != style:
        raise ValueError("저장한 추종 성향과 참조 봇이 일치하지 않습니다.")
    source = _source(style, event_id, now)
    symbol = source["ticker"]
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isalnum():
        raise ValueError("국내 종목코드 검증 실패")
    scaled = live_policy.scaled_quantity(source, follow_pct=policy["follow_pct"], limit_price=limit_price)
    qty = scaled["qty"]
    if qty <= 0 or qty > 1000:
        raise ValueError("소액 파일럿 범위를 벗어나는 주문 수량입니다.")
    _regular_session(now)
    quote, quote_at = _quote(symbol, now)
    price = Decimal(limit_price)
    side = "BUY" if source["side"] == "buy" else "SELL"
    if (side == "BUY" and price > quote * Decimal("1.005")) or (side == "SELL" and price < quote * Decimal("0.995")):
        raise ValueError("지정가가 현재가에서 0.5% 넘게 불리하여 주문을 보류합니다.")
    try:
        account = toss_readonly._verified_account()
    except ValueError:
        raise ValueError("설정된 토스 계좌와 실제 계좌가 일치하지 않습니다.") from None
    if account is None:
        raise ValueError("토스 계좌 목록을 조회하지 못했습니다.")
    cash, holdings_value, position, sellable = _account_values(account, symbol, side)
    base = cash + holdings_value  # USD 자산·미확인 현금을 제외한 국내 위험 계산용 보수적 기준
    if base <= 0:
        raise ValueError("국내 자산 기준금액을 확인하지 못했습니다.")
    notional = price * qty
    reserve = (notional * Decimal("1.01")).quantize(Decimal("1"), rounding=ROUND_CEILING)
    if reserve > PILOT_MAX_ORDER_KRW:
        raise ValueError("파일럿 주문당 10만원 한도를 넘습니다.")
    if reserve > base * _decimal(policy["max_order_pct"]) / 100:
        raise ValueError("설정된 주문당 비중 한도를 넘습니다.")
    if side == "BUY":
        if position + notional > base * _decimal(policy["max_position_pct"]) / 100:
            raise ValueError("설정된 종목 비중 한도를 넘습니다.")
        if cash < reserve or cash - reserve < base * _decimal(policy["min_cash_pct"]) / 100:
            raise ValueError("현금 매수가능액 또는 최소 현금비중이 부족합니다.")
    elif Decimal(qty) > sellable:
        raise ValueError("증권사 매도가능수량보다 주문 수량이 많습니다.")
    daily_budget = min(PILOT_MAX_DAILY_BUY_KRW, base * _decimal(policy["max_daily_buy_pct"]) / 100)
    return {"account": account, "policy": policy, "source": source, "symbol": symbol, "side": side,
            "qty": qty, "price": limit_price, "quote": str(quote), "quote_at": quote_at,
            "cash": str(cash), "sellable": str(sellable), "daily_budget": str(daily_budget),
            "risk_base": str(base), "reserve": str(reserve)}


def preview(uid: int, *, style: str, event_id: int, limit_price: int, now: int | None = None) -> dict:
    at = int(now if now is not None else time.time())
    intent_ledger.expire_prepared(uid, now=at)
    check = _inspect(uid, style, event_id, limit_price, at)
    intent = intent_ledger.prepare(
        uid=uid, account_seq=check["account"], source_style=style, source_event_id=event_id,
        source_event_ts=check["source"]["ts"], market="kr", symbol=check["symbol"],
        side=check["side"], quantity=check["qty"], limit_price=limit_price,
        cash_available=check["cash"], sellable_available=check["sellable"],
        daily_buy_budget=check["daily_budget"], now=at)
    intent_ledger.bind_approval(intent["id"], uid, policy_hash=intent_ledger.policy_fingerprint(check["policy"]),
                                quote_at=check["quote_at"], quote_price=check["quote"],
                                expires_at=at + 60, now=at)
    phrase = f'{check["symbol"]} {"매수" if check["side"] == "BUY" else "매도"} {check["qty"]}주 {limit_price}원'
    return {"ready": True, "intent_id": intent["id"], "expires_at": at + 60,
            "symbol": check["symbol"], "side": check["side"], "quantity": check["qty"],
            "limit_price": limit_price, "max_notional": str(Decimal(check["qty"]) * limit_price),
            "quote_price": check["quote"], "confirmation_phrase": phrase,
            "mode": "manual_kr_limit_pilot", "automatic_copy_enabled": False,
            "note": "보유주식+현금 매수가능액의 국내 위험 기준으로 점검했습니다. 60초 안에 직접 확인해야 주문됩니다."}


def submit(uid: int, *, intent_id: str, confirmation_phrase: str, now: int | None = None) -> dict:
    at = int(now if now is not None else time.time())
    _require_enabled()
    intent = intent_ledger.get(intent_id)
    approval = intent_ledger.approval_get(intent_id, uid)
    if (intent is None or intent["uid"] != uid or intent["status"] != "PREPARED" or not approval
            or at > approval["expires_at"] or at - intent["created"] > 90):
        raise ValueError("승인 시간이 지났거나 이미 제출된 주문입니다.")
    expected = f'{intent["symbol"]} {"매수" if intent["side"] == "BUY" else "매도"} {intent["quantity"]}주 {intent["limit_price"]}원'
    if confirmation_phrase != expected:
        raise ValueError("표시된 종목·방향·수량·가격을 그대로 입력해 승인하세요.")
    check = _inspect(uid, intent["source_style"], intent["source_event_id"], int(intent["limit_price"]), at)
    if (check["account"] != intent["account_seq"] or check["symbol"] != intent["symbol"]
            or check["side"] != intent["side"] or check["qty"] != int(intent["quantity"])):
        raise ValueError("주문 조건이 사전점검 이후 변경되었습니다.")
    if intent_ledger.policy_fingerprint(check["policy"]) != approval["policy_hash"]:
        raise ValueError("추종 설정이 변경되어 다시 점검해야 합니다.")
    claim_at = int(time.time()) if now is None else at
    if claim_at - int(check["quote_at"]) > 30:
        raise ValueError("최종 계좌 조회 중 시세가 오래되어 주문을 보류합니다.")
    claimed = intent_ledger.claim_approved(intent_id, uid, now=claim_at)
    try:
        outcome = toss_order.submit_limit(check["account"], claimed)
    except Exception:
        outcome = {"accepted": False, "reason": "broker submission outcome unknown"}
    if not outcome["accepted"]:
        try:
            intent_ledger.transition(intent_id, "UNKNOWN", evidence=outcome["reason"])
        except Exception:
            pass  # SUBMITTING도 재제출 불가 상태로 남는다.
        return {"status": "UNKNOWN", "intent_id": intent_id,
                "reason": "주문 결과를 확인할 수 없습니다. 토스 앱에서 주문을 확인하기 전에는 다시 제출하지 마세요."}
    try:
        intent_ledger.transition(intent_id, "OPEN", broker_order_id=outcome["order_id"],
                                 evidence="broker_acknowledged")
    except Exception:
        # 브로커는 접수했을 수 있으므로 DB 장애 중 ACK를 잃어도 절대 재제출하지 않는다.
        return {"status": "UNKNOWN", "intent_id": intent_id,
                "reason": "증권사 접수 응답 후 원장 기록에 실패했습니다. 토스 앱에서 확인하고 다시 제출하지 마세요."}
    try:
        settled = intent_ledger.reconcile_known_order(intent_id)
        status = settled["status"]
    except ValueError:
        status = "OPEN"
    return {"status": status, "intent_id": intent_id,
            "reason": "토스 주문 접수 응답을 확인했습니다. 체결 여부는 주문 상태를 다시 조회하세요."}


def order_status(uid: int, intent_id: str) -> dict:
    intent = intent_ledger.get(intent_id)
    if intent is None or intent["uid"] != uid:
        raise ValueError("주문 기록을 찾을 수 없습니다.")
    if intent["broker_order_id"] and intent["status"] in ("OPEN", "PARTIAL", "UNKNOWN"):
        try:
            intent = intent_ledger.reconcile_known_order(intent_id)
        except ValueError:
            pass
    return {"intent_id": intent_id, "symbol": intent["symbol"], "side": intent["side"],
            "quantity": intent["quantity"], "filled_quantity": intent["filled_quantity"],
            "status": intent["status"],
            "unknown_requires_manual_check": intent["status"] in ("UNKNOWN", "SUBMITTING"),
            "automatic_copy_enabled": False}
