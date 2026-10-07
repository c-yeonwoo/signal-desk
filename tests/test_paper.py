"""자체 모의계좌(paper) 브로커 — 유저별 가상 체결·현금·포지션 정합성."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from signal_desk import bot_alerts, config, db, store
from signal_desk.broker import paper

UID = 3


def _seed(monkeypatch, tmp_path, price=70000.0):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "load_price_series", lambda: {"005930": [price]})
    monkeypatch.setattr(store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    db.user_bot_set_seed(UID, 1_000_000.0)


def test_paper_buy_sell_cash_and_positions(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    assert paper.balance(UID)["cash"] == 1_000_000.0

    assert paper.place_order(UID, "005930", "buy", 10, price=70000.0)["order_no"].startswith("PAPER-")
    b = paper.balance(UID)
    assert b["cash"] == 299_544.95                     # 불리한 슬리피지 + 매수 수수료 반영
    assert b["holdings"][0] == {"ticker": "005930", "name": "삼성전자", "qty": 10,
                                "avg_price": 70045.51, "price": 70000.0, "pnl_pct": -0.06}

    paper.place_order(UID, "005930", "sell", 4, price=75000.0)
    b = paper.balance(UID)
    assert b["cash"] == 598_750.27                     # 매도 슬리피지·수수료·거래세 차감
    assert b["holdings"][0]["qty"] == 6


def test_paper_isolated_per_uid(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    db.user_bot_set_seed(9, 1_000_000.0)
    paper.place_order(UID, "005930", "buy", 5, price=70000.0)
    assert len(paper.balance(UID)["holdings"]) == 1      # UID만 보유
    assert paper.balance(9)["holdings"] == [] and paper.balance(9)["cash"] == 1_000_000.0  # 다른 유저 격리


def test_paper_rejects_insufficient(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    assert paper.place_order(UID, "005930", "buy", 100, price=70000.0) is None  # 현금 부족
    assert paper.place_order(UID, "005930", "sell", 1, price=70000.0) is None   # 미보유


def test_paper_pnl_from_price_cache(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=70000.0)
    paper.place_order(UID, "005930", "buy", 5, price=70000.0)
    monkeypatch.setattr(store, "load_price_series", lambda: {"005930": [77000.0]})  # +10%
    h = paper.balance(UID)["holdings"][0]
    assert h["price"] == 77000.0 and h["pnl_pct"] == 9.93


def test_concurrent_buys_cannot_spend_the_same_cash(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    db.kv_set(f"paper_account:{UID}", {"cash": 1000.0, "positions": {}})
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: paper.place_order(UID, "005930", "buy", 1, price=100.0), range(20)))
    filled = [r for r in results if r]
    assert len(filled) == 9  # 비용 포함 1주 >100원; 10주는 현금 초과
    assert len({r["order_no"] for r in filled}) == 9
    bal = paper.balance(UID)
    assert bal["cash"] >= 0 and bal["holdings"][0]["qty"] == 9


def test_concurrent_sells_cannot_sell_the_same_shares_twice(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    db.kv_set(f"paper_account:{UID}", {"cash": 0.0, "positions": {
        "005930": {"name": "삼성전자", "qty": 5, "avg_price": 100.0}}})
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: paper.place_order(UID, "005930", "sell", 1, price=100.0), range(12)))
    assert sum(r is not None for r in results) == 5
    assert paper.balance(UID)["holdings"] == []


def test_bot_fill_writes_balance_trade_and_event_together(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    result = paper.place_order(UID, "005930", "buy", 1, price=100.0, name="삼성전자",
                               reason="SIGNAL", note="테스트", score=2.0,
                               policy_id="execution-123", signal_policy_id="signal-456")
    assert result is not None
    trades = db.bot_trades_recent(UID)
    events = db.execution_events_for_uid(UID, "kr")
    assert len(trades) == len(events) == 1
    assert trades[0]["order_no"] == result["order_no"]
    assert events[0]["payload"]["score"] == 2.0
    assert events[0]["payload"]["execution_policy_id"] == "execution-123"
    assert events[0]["payload"]["signal_policy_id"] == "signal-456"
    # 기존 호출자의 후속 로그 보충은 같은 체결을 두 건으로 세지 않는다.
    db.bot_trade_log(UID, "005930", "삼성전자", "buy", 1, result["fill_price"],
                     "SIGNAL", result["order_no"], note="보충")
    assert len(db.bot_trades_recent(UID)) == 1
    assert db.bot_trades_recent(UID)[0]["note"] == "보충"


def test_bot_fill_preserves_price_evidence_captured_for_reference_price(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    reference_evidence = {"price_basis": "intraday_provisional", "price_session": "2026-10-07",
                          "price_observation_id": "quote-at-sizing"}
    monkeypatch.setattr(store, "live_price_evidence", lambda ticker: {
        "fresh": True, "observation_id": "newer-quote-at-commit"})

    result = paper.place_order(UID, "005930", "buy", 1, price=100.0, name="삼성전자",
                               reason="SIGNAL", event_payload={"price_evidence": reference_evidence})

    event = db.execution_events_for_uid(UID, "kr")[0]
    assert result is not None
    assert event["payload"]["price_evidence"] == reference_evidence


def test_audit_failure_rolls_back_paper_cash_and_position(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    before = paper.balance(UID)
    c = db.conn()
    c.execute("CREATE TRIGGER reject_paper_event BEFORE INSERT ON execution_events "
              "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    c.commit(); c.close()
    with pytest.raises(sqlite3.IntegrityError):
        paper.place_order(UID, "005930", "buy", 1, price=100.0,
                          reason="SIGNAL", note="테스트")
    assert paper.balance(UID) == before
    assert db.bot_trades_recent(UID) == []


def test_concurrent_buys_recheck_position_slots_in_writer(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    db.kv_set(f"paper_account:{UID}", {"cash": 1000.0, "positions": {}})
    policy = {"max_positions": 1, "position_pct": 1.0, "exposure": 1.0}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda t: paper.place_order(
            UID, t, "buy", 1, price=100.0, name=t, reason="SIGNAL", risk_policy=policy),
            ["AAA", "BBB"]))
    assert sum(r is not None for r in results) == 1
    assert len(paper.balance(UID)["holdings"]) == len(db.bot_trades_recent(UID)) == 1


def test_concurrent_buys_recheck_exposure_in_writer(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    db.kv_set(f"paper_account:{UID}", {"cash": 1000.0, "positions": {}})
    policy = {"max_positions": 2, "position_pct": 1.0, "exposure": 0.3}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda t: paper.place_order(
            UID, t, "buy", 2, price=100.0, name=t, reason="SIGNAL", risk_policy=policy),
            ["AAA", "BBB"]))
    assert sum(r is not None for r in results) == 1
    assert len(db.bot_trades_recent(UID)) == 1


def test_buy_revalues_existing_shares_at_new_order_price_for_exposure(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=50.0)
    db.kv_set(f"paper_account:{UID}", {"cash": 500.0, "positions": {
        "005930": {"name": "삼성전자", "qty": 5, "avg_price": 50.0}}})
    policy = {"max_positions": 2, "position_pct": 1.0, "exposure": 0.5}
    assert paper.place_order(UID, "005930", "buy", 1, price=100.0,
                             reason="ADD", risk_policy=policy) is None
    assert paper.balance(UID)["holdings"][0]["qty"] == 5


def test_alert_is_in_outbox_in_same_fill_transaction(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "balanced")
    result = paper.place_order(UID, "005930", "buy", 1, price=100.0, name="삼성전자",
                               reason="SIGNAL", alert_style="balanced")
    assert result is not None
    due = db.notification_outbox_due(now=int(time.time()))
    assert len(due) == 1 and due[0]["dedupe_key"].startswith("paper-fill:")
    assert "매수 삼성전자" in due[0]["text"]


def test_alert_render_failure_rolls_back_fill(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, price=100.0)
    monkeypatch.setattr(config, "telegram_trade_style", lambda: "balanced")
    before = paper.balance(UID)
    monkeypatch.setattr(bot_alerts, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render")))
    with pytest.raises(RuntimeError, match="render"):
        paper.place_order(UID, "005930", "buy", 1, price=100.0,
                          reason="SIGNAL", alert_style="balanced")
    assert paper.balance(UID) == before
    assert db.bot_trades_recent(UID) == []
    assert db.notification_outbox_due(now=9999999999) == []
