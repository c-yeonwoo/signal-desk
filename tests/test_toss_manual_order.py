"""소액 토스 실주문 파일럿은 실제 네트워크 없이 소유자 승인·중복·불명 결과를 검증한다."""

import importlib
import io
import json
import time
import urllib.error
import datetime
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TOSS_MANUAL_ORDER_ENABLED', 'true')
    monkeypatch.setenv('TOSS_ACCOUNT_OWNER', 'owner@x.com')
    from signal_desk import db, api
    importlib.reload(db)
    importlib.reload(api)
    from signal_desk.broker import toss_manual as manual
    now = int(time.time())
    db.live_copy_policy_set(7, {'source_style': 'balanced', 'follow_pct': 100,
        'max_order_pct': 10, 'max_daily_buy_pct': 20, 'max_position_pct': 15,
        'min_cash_pct': 25})
    monkeypatch.setattr(manual.bot, 'ensure_reference_bots', lambda: None)
    monkeypatch.setattr(manual.db, 'bot_trade_get', lambda uid, event, market: {
        'id': event, 'ticker': '005930', 'side': 'buy', 'qty': 1, 'price': 100,
        'ts': now, 'name': 'test'})
    monkeypatch.setattr(manual.advisor_shadow, 'cached_summary', lambda: {})
    monkeypatch.setattr(manual.advisor_shadow, 'decision_status',
                        lambda **kw: {'buy_path_active': True})
    monkeypatch.setattr(manual, '_regular_session', lambda at: None)
    monkeypatch.setattr(manual, '_quote', lambda symbol, at: (Decimal('100'), at))
    monkeypatch.setattr(manual.toss_readonly, '_verified_account', lambda: '1')
    monkeypatch.setattr(manual, '_account_values', lambda *a: (
        Decimal('1200'), Decimal('0'), Decimal('0'), Decimal('0')))
    return manual, db, now


def test_manual_preview_then_single_submission(tmp_path, monkeypatch):
    manual, db, now = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(manual.toss_order, 'submit_limit', lambda account, intent: (
        calls.append((account, intent['client_order_id'])) or
        {'accepted': True, 'order_id': 'broker-1'}))
    monkeypatch.setattr(manual.intent_ledger, 'reconcile_known_order', lambda intent_id: (
        {**manual.intent_ledger.get(intent_id), 'status': 'OPEN'}))
    preview = manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    assert preview['ready'] and preview['quantity'] == 1
    assert preview['confirmation_phrase'] == '005930 매수 1주 100원'
    submitted = manual.submit(7, intent_id=preview['intent_id'],
                              confirmation_phrase=preview['confirmation_phrase'], now=now)
    assert submitted['status'] == 'OPEN' and len(calls) == 1
    with pytest.raises(ValueError, match='이미 제출'):
        manual.submit(7, intent_id=preview['intent_id'],
                      confirmation_phrase=preview['confirmation_phrase'], now=now)
    assert len(calls) == 1
    assert manual.order_status(7, preview['intent_id'])['status'] == 'OPEN'
    with pytest.raises(ValueError):
        manual.order_status(8, preview['intent_id'])


def test_timeout_becomes_unknown_without_retry(tmp_path, monkeypatch):
    manual, _, now = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(manual.toss_order, 'submit_limit', lambda account, intent: (
        calls.append(intent['id']) or {'accepted': False, 'reason': 'timeout'}))
    preview = manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    result = manual.submit(7, intent_id=preview['intent_id'],
                           confirmation_phrase=preview['confirmation_phrase'], now=now)
    assert result['status'] == 'UNKNOWN' and len(calls) == 1
    with pytest.raises(ValueError):
        manual.preview(7, style='balanced', event_id=2, limit_price=100, now=now)
    with pytest.raises(ValueError):
        manual.submit(7, intent_id=preview['intent_id'],
                      confirmation_phrase=preview['confirmation_phrase'], now=now)
    assert len(calls) == 1


def test_policy_change_and_wrong_phrase_block_transport(tmp_path, monkeypatch):
    manual, db, now = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(manual.toss_order, 'submit_limit',
                        lambda *a: pytest.fail('must not submit'))
    preview = manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    with pytest.raises(ValueError, match='그대로 입력'):
        manual.submit(7, intent_id=preview['intent_id'], confirmation_phrase='주문', now=now)
    db.live_copy_policy_set(7, {'follow_pct': 50})
    with pytest.raises(ValueError):
        manual.submit(7, intent_id=preview['intent_id'],
                      confirmation_phrase=preview['confirmation_phrase'], now=now)


def test_runtime_gate_requires_explicit_switch_and_production_volume(monkeypatch):
    from signal_desk.broker import toss_manual as manual
    monkeypatch.delenv('TOSS_MANUAL_ORDER_ENABLED', raising=False)
    monkeypatch.setattr(manual.config, 'is_prod', lambda: False)
    assert not manual.availability()['enabled']
    monkeypatch.setenv('TOSS_MANUAL_ORDER_ENABLED', 'true')
    monkeypatch.setattr(manual.config, 'is_prod', lambda: True)
    monkeypatch.delenv('RAILWAY_VOLUME_MOUNT_PATH', raising=False)
    assert 'persistent_ledger_volume_unverified' in manual.availability()['blockers']


def test_order_post_payload_and_http_failure_never_retries(monkeypatch):
    from signal_desk.broker import toss_order
    monkeypatch.setattr(toss_order.toss, '_access_token', lambda: 'test-token')
    intent = {'status': 'SUBMITTING', 'account_seq': '1', 'client_order_id': 'key-1',
              'symbol': '005930', 'side': 'BUY', 'quantity': '1', 'limit_price': '100'}
    calls = []
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"result":{"orderId":"broker-1","clientOrderId":"key-1"}}'
    def accepted(req, timeout):
        calls.append(req)
        assert req.get_method() == 'POST'
        payload = json.loads(req.data)
        assert payload == {'clientOrderId':'key-1','symbol':'005930','side':'BUY',
                           'orderType':'LIMIT','timeInForce':'DAY','quantity':'1','price':'100'}
        return Resp()
    monkeypatch.setattr(toss_order.urllib.request, 'urlopen', accepted)
    assert toss_order.submit_limit('1', intent)['order_id'] == 'broker-1'
    def unknown(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 500, 'error', None, io.BytesIO(b'private'))
    monkeypatch.setattr(toss_order.urllib.request, 'urlopen', unknown)
    assert toss_order.submit_limit('1', intent)['unknown'] is True
    assert len(calls) == 2  # 각 호출 1회씩, HTTP 오류 재시도 없음


def test_http_owner_and_csrf_gate_before_broker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TOSS_ACCOUNT_OWNER', 'owner@x.com')
    from signal_desk import db, api
    importlib.reload(db)
    importlib.reload(api)
    from signal_desk.broker import toss_manual as manual
    monkeypatch.setattr(manual, 'preview', lambda *a, **k: pytest.fail('no broker call'))
    client = TestClient(api.app)
    client.post('/api/auth/signup', json={'email':'guest@x.com','pw':'abcdef'})
    assert client.post('/api/live/toss-manual/preview', json={}).status_code == 403
    client.post('/api/auth/signup', json={'email':'owner@x.com','pw':'abcdef'})
    assert client.post('/api/live/toss-manual/preview', json={}).status_code == 403


def test_http_manual_preview_requires_same_origin_and_custom_header(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TOSS_ACCOUNT_OWNER', 'owner@x.com')
    from signal_desk import db, api
    importlib.reload(db)
    importlib.reload(api)
    from signal_desk.broker import toss_manual as manual
    calls = []
    monkeypatch.setattr(manual, 'preview', lambda uid, **kw: (
        calls.append((uid, kw)) or {'ready': True, 'intent_id': 'test'}))
    client = TestClient(api.app)
    client.post('/api/auth/signup', json={'email':'owner@x.com','pw':'abcdef'})
    data = {'source_style':'balanced','source_event_id':1,'limit_price':100}
    headers = {'X-Signal-Desk-Order':'manual','Origin':'https://evil.example'}
    assert client.post('/api/live/toss-manual/preview', json=data, headers=headers).status_code == 403
    headers['Origin'] = 'http://testserver'
    assert client.post('/api/live/toss-manual/preview', json=data, headers=headers).json()['ready'] is True
    assert len(calls) == 1 and calls[0][1]['limit_price'] == 100


def test_calendar_quote_and_account_fail_closed(monkeypatch):
    from signal_desk.broker import toss_manual as manual
    kst = ZoneInfo('Asia/Seoul')
    at = int(datetime.datetime(2026, 9, 28, 10, 0, tzinfo=kst).timestamp())
    start = datetime.datetime(2026, 9, 28, 9, 0, tzinfo=kst).isoformat()
    cutoff = datetime.datetime(2026, 9, 28, 15, 20, tzinfo=kst).isoformat()
    monkeypatch.setattr(manual.toss, 'kr_market_calendar', lambda: {'today': {
        'date': '2026-09-28', 'integrated': {'regularMarket': {
            'startTime': start, 'singlePriceAuctionStartTime': cutoff}}}})
    manual._regular_session(at)
    with pytest.raises(ValueError, match='정규장'):
        manual._regular_session(at - 3601)
    monkeypatch.setattr(manual.toss, 'price_quote', lambda symbol: {
        'symbol': symbol, 'currency': 'KRW', 'lastPrice': '100',
        'timestamp': datetime.datetime.fromtimestamp(at - 5, kst).isoformat()})
    assert manual._quote('005930', at)[0] == Decimal('100')
    with pytest.raises(ValueError, match='오래'):
        manual._quote('005930', at + 40)
    monkeypatch.setattr(manual.toss, 'buying_power', lambda *a: {
        'currency': 'KRW', 'cashBuyingPower': '1200'})
    monkeypatch.setattr(manual.toss, 'open_orders', lambda *a: {
        'orders': [], 'nextCursor': None, 'hasNext': False})
    monkeypatch.setattr(manual.toss, 'holdings', lambda *a: {'items': [{
        'symbol': '005930', 'marketCountry': 'KR', 'currency': 'KRW',
        'marketValue': {'amount': '300'}}]})
    assert manual._account_values('1', '005930', 'BUY')[:3] == (
        Decimal('1200'), Decimal('300'), Decimal('300'))
    monkeypatch.setattr(manual.toss, 'open_orders', lambda *a: {'orders': [{'orderId': 'x'}]})
    with pytest.raises(ValueError, match='진행 중 주문'):
        manual._account_values('1', '005930', 'BUY')


def test_absolute_cap_and_expired_approval_block_transport(tmp_path, monkeypatch):
    manual, _, now = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(manual.toss_order, 'submit_limit',
                        lambda *a: pytest.fail('must not submit'))
    # 10만원 주문 한도를 비율보다 먼저 확인한다.
    monkeypatch.setattr(manual, '_account_values', lambda *a: (
        Decimal('10000000'), Decimal('0'), Decimal('0'), Decimal('0')))
    monkeypatch.setattr(manual.db, 'bot_trade_get', lambda uid, event, market: {
        'id': event, 'ticker': '005930', 'side': 'buy', 'qty': 1000, 'price': 100,
        'ts': now, 'name': 'test'})
    with pytest.raises(ValueError, match='파일럿'):
        manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    monkeypatch.setattr(manual.db, 'bot_trade_get', lambda uid, event, market: {
        'id': event, 'ticker': '005930', 'side': 'buy', 'qty': 1, 'price': 100,
        'ts': now, 'name': 'test'})
    p = manual.preview(7, style='balanced', event_id=2, limit_price=100, now=now)
    with pytest.raises(ValueError, match='승인 시간'):
        manual.submit(7, intent_id=p['intent_id'], confirmation_phrase=p['confirmation_phrase'], now=now+61)


def test_parallel_confirmation_submits_only_once(tmp_path, monkeypatch):
    manual, _, now = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(manual.toss_order, 'submit_limit', lambda account, intent: (
        calls.append(intent['id']) or {'accepted': False, 'reason': 'timeout'}))
    p = manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    def confirm():
        try:
            return manual.submit(7, intent_id=p['intent_id'],
                                 confirmation_phrase=p['confirmation_phrase'], now=now)['status']
        except ValueError:
            return 'BLOCKED'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == ['BLOCKED', 'UNKNOWN']
    assert len(calls) == 1


def test_second_prepared_intent_blocked_after_first_becomes_unknown(tmp_path, monkeypatch):
    manual, _, now = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(manual.toss_order, 'submit_limit', lambda account, intent: (
        calls.append(intent['id']) or {'accepted': False, 'reason': 'timeout'}))
    first = manual.preview(7, style='balanced', event_id=1, limit_price=100, now=now)
    second = manual.preview(7, style='balanced', event_id=2, limit_price=100, now=now)
    assert manual.submit(7, intent_id=first['intent_id'],
                         confirmation_phrase=first['confirmation_phrase'], now=now)['status'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='another live order'):
        manual.submit(7, intent_id=second['intent_id'],
                      confirmation_phrase=second['confirmation_phrase'], now=now)
    assert len(calls) == 1
