"""실주문 준비용 내구성 원장. 주문 transport/활성화 API와 의도적으로 분리한다.

증권사 매수가능액·매도가능수량과 별도로 서비스가 준비 중인 의도를 원자적으로 예약한다.
UNKNOWN은 타임아웃의 안전한 상태이며 멱등성 키 만료 후 자동 재전송 대상이 아니다.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from zoneinfo import ZoneInfo

from signal_desk import db, strategy

_ACTIVE = ("PREPARED", "SUBMITTING", "UNKNOWN", "OPEN", "PARTIAL")
_TRANSITIONS = {
    "PREPARED": {"CANCELED", "SUBMITTING"},
    "SUBMITTING": {"UNKNOWN", "OPEN", "PARTIAL", "FILLED", "REJECTED"},
    "UNKNOWN": {"OPEN", "PARTIAL", "FILLED", "CANCELED", "REJECTED"},
    "OPEN": {"UNKNOWN", "PARTIAL", "FILLED", "CANCELED", "REJECTED"},
    "PARTIAL": {"UNKNOWN", "PARTIAL", "FILLED", "CANCELED"},
}
_SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")
_POLICY_FIELDS = ("source_style", "follow_pct", "max_order_pct", "max_daily_buy_pct",
                  "max_position_pct", "min_cash_pct")


def policy_fingerprint(policy: dict) -> str:
    return hashlib.sha256(json.dumps({k: policy[k] for k in _POLICY_FIELDS},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _amount(value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid amount") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("invalid amount")
    return result


def _row(row) -> dict:
    keys = ("id", "uid", "broker", "account_seq", "source_style", "source_event_id", "market",
            "symbol", "side", "quantity", "limit_price", "reserve_cash", "reserve_quantity",
            "filled_quantity", "budget_date", "status", "client_order_id", "broker_order_id",
            "created", "updated")
    return dict(zip(keys, row))


def _get(c, intent_id: str) -> dict | None:
    row = c.execute("SELECT * FROM live_order_intents WHERE id=?", (intent_id,)).fetchone()
    return _row(row) if row else None


def _durable_conn():
    c = db.conn()
    # 주문 선점 기록은 프로세스/호스트 장애 직후에도 남아야 한다.
    c.execute("PRAGMA synchronous=FULL")
    return c


def prepare(*, uid: int, account_seq: str, source_style: str, source_event_id: int,
            source_event_ts: int,
            market: str, symbol: str, side: str, quantity: object, limit_price: object,
            cash_available: object = 0, sellable_available: object = 0,
            daily_buy_budget: object = 0, now: int | None = None) -> dict:
    """검증된 외부 조회값을 받아 로컬 중복·예약을 직렬화한다. 정책 승인/주문 허가는 아니다."""
    if (isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0
            or isinstance(source_event_id, bool) or not isinstance(source_event_id, int)
            or source_event_id <= 0 or isinstance(source_event_ts, bool)
            or not isinstance(source_event_ts, int) or source_style not in strategy.STYLES
            or market not in ("kr", "us") or side not in ("BUY", "SELL")
            or not _SYMBOL.fullmatch(symbol or "") or not str(account_seq).isascii()
            or not str(account_seq).isdecimal() or int(account_seq) <= 0):
        raise ValueError("invalid intent identity")
    qty, price = _amount(quantity, positive=True), _amount(limit_price, positive=True)
    if qty != qty.to_integral_value() or (market == "kr" and price != price.to_integral_value()):
        raise ValueError("only whole-share limit orders are staged")
    if market == "us" and price.as_tuple().exponent < -2:
        raise ValueError("US limit price precision exceeds cents")
    if market == "kr" and not re.fullmatch(r"[A-Za-z0-9]{6}", symbol):
        raise ValueError("invalid Korean symbol")
    cash, sellable, daily_budget = (_amount(cash_available), _amount(sellable_available),
                                     _amount(daily_buy_budget))
    reserve_cash = (qty * price * Decimal("1.01")).quantize(
        Decimal("1") if market == "kr" else Decimal("0.01"), rounding=ROUND_CEILING) if side == "BUY" else Decimal(0)
    reserve_qty = qty if side == "SELL" else Decimal(0)
    at = int(now if now is not None else time.time())
    if not 0 <= at - source_event_ts <= 15 * 60:
        raise ValueError("stale or future reference event")
    day = datetime.datetime.fromtimestamp(at, ZoneInfo("Asia/Seoul")).date().isoformat()
    identity = (uid, "toss", str(int(account_seq)), source_style, source_event_id)
    c = _durable_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM live_order_intents WHERE uid=? AND broker='toss' AND account_seq=? "
                     "AND status IN ('SUBMITTING','UNKNOWN') LIMIT 1", (uid, identity[2])).fetchone():
            raise ValueError("unresolved order blocks new intents")
        existing = c.execute("SELECT id FROM live_order_intents WHERE uid=? AND broker=? AND account_seq=? "
                             "AND source_style=? AND source_event_id=?", identity).fetchone()
        if existing:
            result = _get(c, existing[0])
            if (result["market"], result["symbol"], result["side"], result["quantity"], result["limit_price"]) != (
                    market, symbol, side, str(qty), str(price)):
                raise ValueError("source event already staged with different terms")
            c.commit()
            return result
        rows = [_row(row) for row in c.execute("SELECT * FROM live_order_intents WHERE uid=? AND broker='toss' "
                                                "AND account_seq=? AND market=? AND status IN (?,?,?,?,?)",
                                                (uid, identity[2], market, *_ACTIVE)).fetchall()]
        if side == "BUY":
            locally_reserved = sum((Decimal(r["reserve_cash"]) *
                                    (Decimal(r["quantity"]) - Decimal(r["filled_quantity"])) /
                                    Decimal(r["quantity"]) for r in rows if r["side"] == "BUY"), Decimal(0))
            if reserve_cash + locally_reserved > cash:
                raise ValueError("cash buying power is insufficient after local reservations")
            committed = sum((Decimal(r[0]) for r in c.execute(
                "SELECT reserve_cash FROM live_order_intents WHERE uid=? AND broker='toss' "
                "AND account_seq=? AND market=? AND budget_date=? AND side='BUY' "
                "AND (status NOT IN ('REJECTED','CANCELED') OR filled_quantity!='0')",
                (uid, identity[2], market, day)).fetchall()), Decimal(0))
            if reserve_cash + committed > daily_budget:
                raise ValueError("daily buy budget is exhausted")
        else:
            locally_reserved = sum((Decimal(r["reserve_quantity"]) - Decimal(r["filled_quantity"])
                                    for r in rows if r["side"] == "SELL" and r["symbol"] == symbol), Decimal(0))
            if reserve_qty + locally_reserved > sellable:
                raise ValueError("sellable quantity is insufficient after local reservations")
        intent_id, client_id = uuid.uuid4().hex, uuid.uuid4().hex
        c.execute("INSERT INTO live_order_intents(id,uid,broker,account_seq,source_style,source_event_id,"
                  "market,symbol,side,quantity,limit_price,reserve_cash,reserve_quantity,budget_date,status,"
                  "client_order_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (intent_id, uid, "toss", identity[2], source_style, source_event_id, market, symbol,
                   side, str(qty), str(price), str(reserve_cash), str(reserve_qty), day,
                   "PREPARED", client_id, at, at))
        c.execute("INSERT INTO live_order_intent_events(intent_id,event_key,from_status,to_status,"
                  "filled_quantity,evidence,created) VALUES(?,?,?,?,?,?,?)",
                  (intent_id, intent_id + ":prepared", None, "PREPARED", "0", "local_preparation", at))
        c.commit()
        return _get(c, intent_id)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def transition(intent_id: str, to_status: str, *, evidence: str,
               broker_order_id: str | None = None, filled_quantity: object = 0,
               event_key: str | None = None, now: int | None = None) -> dict:
    """주문 상태 감사 전이. UNKNOWN에서 제출 상태로 돌아가는 전이는 존재하지 않는다."""
    if (not isinstance(evidence, str) or not evidence or len(evidence) > 200
            or to_status not in set().union(*_TRANSITIONS.values())):
        raise ValueError("invalid transition evidence")
    filled = _amount(filled_quantity)
    c = _durable_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _get(c, intent_id)
        if row is None or to_status not in _TRANSITIONS.get(row["status"], set()):
            raise ValueError("invalid intent transition")
        if filled < Decimal(row["filled_quantity"]) or filled > Decimal(row["quantity"]):
            raise ValueError("filled quantity cannot regress or exceed order")
        if row["market"] == "kr" and filled != filled.to_integral_value():
            raise ValueError("Korean share fills must be whole")
        if row["status"] == "PREPARED" and filled != 0:
            raise ValueError("unsubmitted intent cannot be filled")
        if to_status == "FILLED" and filled != Decimal(row["quantity"]):
            raise ValueError("full fill requires full quantity")
        broker_id = broker_order_id or row["broker_order_id"]
        if (to_status not in ("SUBMITTING", "UNKNOWN") and row["status"] != "PREPARED"
                and not broker_id):
            raise ValueError("broker order evidence is required")
        if row["broker_order_id"] and broker_order_id and row["broker_order_id"] != broker_order_id:
            raise ValueError("broker order id changed")
        at = int(now if now is not None else time.time())
        c.execute("UPDATE live_order_intents SET status=?,filled_quantity=?,broker_order_id=?,updated=? WHERE id=?",
                  (to_status, str(filled), broker_id, at, intent_id))
        c.execute("INSERT INTO live_order_intent_events(intent_id,event_key,from_status,to_status,"
                  "filled_quantity,broker_order_id,evidence,created) VALUES(?,?,?,?,?,?,?,?)",
                  (intent_id, event_key or uuid.uuid4().hex, row["status"], to_status,
                   str(filled), broker_id, evidence, at))
        c.commit()
        return _get(c, intent_id)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def get(intent_id: str) -> dict | None:
    c = db.conn()
    try:
        return _get(c, intent_id)
    finally:
        c.close()


def bind_approval(intent_id: str, uid: int, *, policy_hash: str, quote_at: int,
                  quote_price: str, expires_at: int, now: int | None = None) -> dict:
    """사전점검 결과를 최초 한 번만 바인딩한다. 재미리보기로 만료를 연장하지 않는다."""
    at = int(now if now is not None else time.time())
    if (not isinstance(policy_hash, str) or len(policy_hash) != 64 or expires_at <= at
            or expires_at - at > 90 or quote_at > at or at - quote_at > 30):
        raise ValueError("approval freshness invalid")
    c = _durable_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _get(c, intent_id)
        if row is None or row["uid"] != uid or row["status"] != "PREPARED":
            raise ValueError("intent is not approvable")
        c.execute("INSERT OR IGNORE INTO live_order_approvals("
                  "intent_id,uid,policy_hash,quote_at,quote_price,expires_at,created) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (intent_id, uid, policy_hash, quote_at, quote_price, expires_at, at))
        bound = c.execute("SELECT policy_hash,quote_at,quote_price,expires_at FROM live_order_approvals "
                          "WHERE intent_id=? AND uid=?", (intent_id, uid)).fetchone()
        c.commit()
        if bound != (policy_hash, quote_at, quote_price, expires_at):
            raise ValueError("existing approval cannot be refreshed")
        return {"policy_hash": bound[0], "quote_at": bound[1],
                "quote_price": bound[2], "expires_at": bound[3]}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def approval_get(intent_id: str, uid: int) -> dict | None:
    c = db.conn()
    try:
        row = c.execute("SELECT policy_hash,quote_at,quote_price,expires_at FROM live_order_approvals "
                        "WHERE intent_id=? AND uid=?", (intent_id, uid)).fetchone()
        return {"policy_hash": row[0], "quote_at": row[1], "quote_price": row[2],
                "expires_at": row[3]} if row else None
    finally:
        c.close()


def claim_approved(intent_id: str, uid: int, *, now: int | None = None) -> dict:
    """만료·정책 불변성을 DB 트랜잭션 안에서 확인하고 단일 제출자만 선점한다."""
    at = int(now if now is not None else time.time())
    c = _durable_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        intent = _get(c, intent_id)
        approval = c.execute("SELECT policy_hash,expires_at FROM live_order_approvals "
                             "WHERE intent_id=? AND uid=?", (intent_id, uid)).fetchone()
        if (intent is None or intent["uid"] != uid or intent["status"] != "PREPARED"
                or not approval or approval[1] < at or at - intent["created"] > 90):
            raise ValueError("approval expired or intent already claimed")
        if c.execute("SELECT 1 FROM live_order_intents WHERE uid=? AND broker=? AND account_seq=? "
                     "AND id!=? AND status IN ('SUBMITTING','UNKNOWN','OPEN','PARTIAL') LIMIT 1",
                     (uid, intent["broker"], intent["account_seq"], intent_id)).fetchone():
            raise ValueError("another live order must be reconciled first")
        policy_row = c.execute("SELECT " + ",".join(_POLICY_FIELDS) +
                               " FROM live_copy_policies WHERE uid=?", (uid,)).fetchone()
        if not policy_row or policy_fingerprint(dict(zip(_POLICY_FIELDS, policy_row))) != approval[0]:
            raise ValueError("copy policy changed after preview")
        c.execute("UPDATE live_order_intents SET status='SUBMITTING',updated=? WHERE id=?", (at, intent_id))
        c.execute("INSERT INTO live_order_intent_events(intent_id,event_key,from_status,to_status,"
                  "filled_quantity,evidence,created) VALUES(?,?,?,?,?,?,?)",
                  (intent_id, intent_id + ":submitting", "PREPARED", "SUBMITTING", "0",
                   "owner_manual_approval", at))
        c.commit()
        return _get(c, intent_id)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def expire_prepared(uid: int, *, now: int | None = None) -> int:
    at = int(now if now is not None else time.time())
    c = db.conn()
    try:
        ids = [r[0] for r in c.execute("SELECT i.id FROM live_order_intents i "
                                      "JOIN live_order_approvals a ON a.intent_id=i.id "
                                      "WHERE i.uid=? AND i.status='PREPARED' AND a.expires_at<?",
                                      (uid, at)).fetchall()]
    finally:
        c.close()
    expired = 0
    for intent_id in ids:
        try:
            transition(intent_id, "CANCELED", evidence="approval_expired", now=at)
            expired += 1
        except ValueError:
            pass  # 다른 요청에서 이미 제출 상태로 선점한 경우 원장 상태를 절대 덮지 않는다.
    return expired


def recent(uid: int, limit: int = 20) -> list[dict]:
    c = db.conn()
    try:
        rows = c.execute("SELECT id,symbol,side,quantity,limit_price,filled_quantity,status,created,updated "
                         "FROM live_order_intents WHERE uid=? ORDER BY created DESC,id DESC LIMIT ?",
                         (uid, min(50, max(1, limit)))).fetchall()
        keys = ("intent_id", "symbol", "side", "quantity", "limit_price", "filled_quantity",
                "status", "created", "updated")
        return [dict(zip(keys, row)) for row in rows]
    finally:
        c.close()


def reconcile_known_order(intent_id: str) -> dict:
    """알려진 주문 ID만 GET으로 대조. ID 없는 UNKNOWN은 수동 확인 전까지 유지한다."""
    from signal_desk.broker import toss_readonly
    from signal_desk.ingest import toss

    intent = get(intent_id)
    if intent is None or not intent["broker_order_id"]:
        raise ValueError("unknown broker order id; manual reconciliation required")
    account = toss_readonly._verified_account()
    if account is None or account != intent["account_seq"]:
        raise ValueError("broker account verification failed")
    order = toss.order_detail(account, intent["broker_order_id"])
    if not isinstance(order, dict):
        raise ValueError("broker order detail unavailable")
    execution = order.get("execution")
    if (order.get("orderId") != intent["broker_order_id"]
            or order.get("symbol") != intent["symbol"] or order.get("side") != intent["side"]
            or order.get("currency") != ("KRW" if intent["market"] == "kr" else "USD")
            or not isinstance(execution, dict)
            or _amount(order.get("quantity")) != Decimal(intent["quantity"])):
        raise ValueError("broker order identity mismatch")
    states = {"PENDING": "OPEN", "PENDING_CANCEL": "OPEN", "PENDING_REPLACE": "OPEN",
              "PARTIAL_FILLED": "PARTIAL", "FILLED": "FILLED", "CANCELED": "CANCELED",
              "REJECTED": "REJECTED"}
    state = states.get(order.get("status"))
    if state is None:
        raise ValueError("broker status needs manual reconciliation")
    filled = _amount(execution.get("filledQuantity"))
    if intent["status"] == state and Decimal(intent["filled_quantity"]) == filled:
        return intent
    return transition(intent_id, state, evidence="broker_order_detail",
                      broker_order_id=intent["broker_order_id"], filled_quantity=filled,
                      event_key=f"{intent_id}:{state}:{filled}")
