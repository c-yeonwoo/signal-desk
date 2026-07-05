"""자체 모의계좌(paper) 브로커 — 유저별 가상 체결·현금·포지션 정합성."""

from signal_desk import db, store
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

    assert paper.place_order(UID, "005930", "buy", 10, price=70000.0)["order_no"] == "PAPER"
    b = paper.balance(UID)
    assert b["cash"] == 300_000.0                      # 100만 − 70만
    assert b["holdings"][0] == {"ticker": "005930", "name": "삼성전자", "qty": 10,
                                "avg_price": 70000.0, "price": 70000.0, "pnl_pct": 0.0}

    paper.place_order(UID, "005930", "sell", 4, price=75000.0)
    b = paper.balance(UID)
    assert b["cash"] == 600_000.0                      # 30만 + 4×75000
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
    assert h["price"] == 77000.0 and h["pnl_pct"] == 10.0
