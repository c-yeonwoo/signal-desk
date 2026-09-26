"""토스 직접 계좌 조회: 계좌 일치, 소유자 분리, 파싱 실패 시 보수적 응답."""

import importlib

from fastapi.testclient import TestClient


def test_adapter_uses_only_get_and_account_header(monkeypatch):
    from signal_desk.ingest import toss
    calls = []

    def fake_get(url, *, headers):
        calls.append((url, headers))
        if url.endswith('/accounts'):
            return {"result": [{"accountSeq": 1, "accountNo": "private", "accountType": "BROKERAGE"}]}
        if 'buying-power' in url:
            return {"result": {"currency": "USD" if 'USD' in url else "KRW", "cashBuyingPower": "10"}}
        if '/orders?' in url:
            return {"result": {"orders": [], "nextCursor": None, "hasNext": False}}
        return {"result": {"sellableQuantity": "2"}}

    monkeypatch.setattr(toss, '_authorized_get', fake_get)
    assert toss.accounts()[0]['accountSeq'] == 1
    assert toss.buying_power('1', 'KRW')['cashBuyingPower'] == '10'
    assert toss.open_orders('1')['orders'] == []
    assert toss.sellable_quantity('1', 'AAPL')['sellableQuantity'] == '2'
    assert all(headers.get('X-Tossinvest-Account') == '1' for _, headers in calls[1:])
    assert all('/api/v1/' in url for url, _ in calls)


def test_snapshot_fails_closed_on_mismatch_and_bad_order(monkeypatch):
    from signal_desk.broker import toss_readonly as ro
    monkeypatch.setenv('TOSS_ACCOUNT', '1')
    monkeypatch.setattr(ro.toss, 'accounts', lambda: [{"accountSeq": 2, "accountType": "BROKERAGE"}])
    assert ro.snapshot()['ready'] is False
    monkeypatch.setattr(ro.toss, 'accounts', lambda: [{"accountSeq": 1, "accountType": "BROKERAGE",
                                                    "accountNo": "private"}])
    monkeypatch.setattr(ro.toss, 'buying_power', lambda account, currency: {"currency": currency,
                                                                           "cashBuyingPower": "100"})
    monkeypatch.setattr(ro.toss, 'open_orders', lambda account: {"orders": [{"symbol": "AAPL",
        "side": "BUY", "quantity": "nan", "execution": {"filledQuantity": "0"}}]})
    assert ro.snapshot()['ready'] is False
    monkeypatch.setattr(ro.toss, 'open_orders', lambda account: {"orders": [{"symbol": "AAPL",
        "side": "BUY", "status": "PENDING", "currency": "USD", "quantity": "3",
        "execution": {"filledQuantity": "1"}}]})
    result = ro.snapshot()
    assert result['ready'] is True and result['open_order_count'] == 1
    assert result['order_transmission_enabled'] is False
    assert 'private' not in str(result)
    monkeypatch.setattr(ro.toss, 'sellable_quantity', lambda account, symbol: None)
    assert ro.sellable('AAPL')['ready'] is False  # mocked transport has no sellable response


def test_owner_gate_prevents_any_broker_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TOSS_ACCOUNT_OWNER', 'owner@x.com')
    from signal_desk import db, api
    importlib.reload(db)
    importlib.reload(api)
    from signal_desk.broker import toss_readonly as ro
    monkeypatch.setattr(ro, 'snapshot', lambda: (_ for _ in ()).throw(AssertionError('broker read')))
    monkeypatch.setattr(ro, 'sellable', lambda symbol: (_ for _ in ()).throw(AssertionError('broker read')))
    client = TestClient(api.app)
    client.post('/api/auth/signup', json={"email": "guest@x.com", "pw": "abcdef"})
    assert client.get('/api/my-broker-account').status_code == 403
    assert client.get('/api/my-broker-sellable?symbol=AAPL').status_code == 403
