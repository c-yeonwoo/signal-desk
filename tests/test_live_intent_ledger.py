"""잠긴 실주문 intent 원장: 원자적 예약·중복·UNKNOWN·증권사 상세 대조."""

import importlib

import pytest


def _ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    importlib.reload(db)
    from signal_desk.broker import intent_ledger
    return intent_ledger, db


def _buy(ledger, event=1, **kw):
    return ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=event,
                          source_event_ts=1_800_000_000,
                          market='kr', symbol='005930', side='BUY', quantity='1', limit_price='100',
                          cash_available='150', daily_buy_budget='200', now=1_800_000_000, **kw)


def test_prepare_dedupes_and_atomically_reserves(tmp_path, monkeypatch):
    ledger, db = _ledger(tmp_path, monkeypatch)
    first = _buy(ledger)
    assert first['status'] == 'PREPARED' and first['reserve_cash'] == '101'
    assert len(first['client_order_id']) == 32
    assert _buy(ledger)['id'] == first['id']
    with pytest.raises(ValueError, match='reservations'):
        _buy(ledger, event=2)
    assert ledger.get(first['id'])['status'] == 'PREPARED'
    c = db.conn()
    assert c.execute('SELECT COUNT(*) FROM live_order_intents').fetchone()[0] == 1
    c.close()
    ledger.transition(first['id'], 'CANCELED', evidence='local_cancel')
    second = _buy(ledger, event=2)
    assert second['id'] != first['id']
    with pytest.raises(ValueError, match='different terms'):
        ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=2,
                       source_event_ts=1_800_000_000,
                       market='kr', symbol='005930', side='BUY', quantity='2', limit_price='100',
                       cash_available='1000', daily_buy_budget='1000', now=1_800_000_000)
    with pytest.raises(ValueError, match='different terms'):
        ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=2,
                       source_event_ts=1_800_000_000,
                       market='kr', symbol='005930', side='SELL', quantity='1', limit_price='100',
                       sellable_available='10', now=1_800_000_000)


def test_unknown_keeps_reservation_and_forbids_resubmit(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path, monkeypatch)
    first = _buy(ledger)
    ledger.transition(first['id'], 'SUBMITTING', evidence='internal_claim')
    ledger.transition(first['id'], 'UNKNOWN', evidence='transport_timeout')
    assert ledger.get(first['id'])['status'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='unresolved order'):
        _buy(ledger, event=2)
    with pytest.raises(ValueError, match='invalid intent transition'):
        ledger.transition(first['id'], 'SUBMITTING', evidence='unsafe_retry')
    with pytest.raises(ValueError, match='manual reconciliation'):
        ledger.reconcile_known_order(first['id'])
    with pytest.raises(ValueError, match='broker order evidence'):
        ledger.transition(first['id'], 'REJECTED', evidence='assumption')


def test_known_order_reconciles_without_resending(tmp_path, monkeypatch):
    ledger, db = _ledger(tmp_path, monkeypatch)
    first = _buy(ledger)
    ledger.transition(first['id'], 'SUBMITTING', evidence='internal_claim')
    ledger.transition(first['id'], 'UNKNOWN', evidence='transport_timeout', broker_order_id='broker-1')
    from signal_desk.broker import toss_readonly
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss_readonly, '_verified_account', lambda: '1')
    monkeypatch.setattr(toss, 'order_detail', lambda account, order_id: {
        'orderId': order_id, 'symbol': '005930', 'side': 'BUY', 'currency': 'KRW',
        'quantity': '1', 'status': 'FILLED', 'execution': {'filledQuantity': '1'}})
    assert ledger.reconcile_known_order(first['id'])['status'] == 'FILLED'
    assert ledger.reconcile_known_order(first['id'])['status'] == 'FILLED'
    c = db.conn()
    assert c.execute('SELECT COUNT(*) FROM live_order_intent_events WHERE intent_id=?',
                     (first['id'],)).fetchone()[0] == 4
    c.close()
    with pytest.raises(ValueError, match='daily buy budget'):
        _buy(ledger, event=2)  # 체결된 매수는 당일 한도에 계속 포함


def test_sellable_quantity_reserved_per_symbol(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path, monkeypatch)
    def sell(event):
        return ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=event,
                              source_event_ts=1_800_000_000,
                              market='us', symbol='AAPL', side='SELL', quantity='2', limit_price='100',
                              sellable_available='3', now=1_800_000_000)
    first = sell(1)
    assert first['reserve_quantity'] == '2'
    with pytest.raises(ValueError, match='sellable quantity'):
        sell(2)
    ledger.transition(first['id'], 'CANCELED', evidence='local_cancel')
    assert sell(2)['status'] == 'PREPARED'


def test_stale_source_and_nonfinite_terms_are_rejected(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='stale'):
        ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=1,
                       source_event_ts=1_800_000_000 - 901, market='kr', symbol='005930',
                       side='BUY', quantity='1', limit_price='100', cash_available='1000',
                       daily_buy_budget='1000', now=1_800_000_000)
    with pytest.raises(ValueError, match='invalid amount'):
        ledger.prepare(uid=7, account_seq='1', source_style='balanced', source_event_id=1,
                       source_event_ts=1_800_000_000, market='kr', symbol='005930',
                       side='BUY', quantity='1', limit_price='nan', cash_available='1000',
                       daily_buy_budget='1000', now=1_800_000_000)
