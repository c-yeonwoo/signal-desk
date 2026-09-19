from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import copy
from types import SimpleNamespace
import zlib

import exchange_calendars as xcals
import pytest

from signal_desk import db
from signal_desk.signals import portfolio_audit as audit, portfolio_counterfactual as cf


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def fixture_inputs():
    days = [d.date().isoformat() for d in xcals.get_calendar('XKRX').sessions_in_range('2026-05-01', '2026-09-18')]
    prices = [100 + i + (i % 3) for i in range(len(days))]
    profile = dict(cash=1000, min_cash_pct=10, max_single_position_pct=40,
                   max_sector_pct=50, max_cluster_pct=50)
    return dict(rows=[dict(ticker='005930', name='Test', qty=10, price=prices[-1], value=10*prices[-1],
                          sector='tech', history_ready=True, price_as_of=days[-1], entry_allowed=False)],
                universe=[dict(ticker='005930', name='Test', sector='tech')], signal_by_ticker={},
                prices={'005930': prices}, dates_by={'005930': days}, profile=profile, market='kr')


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setattr(audit, 'utc_now', lambda: instant('2026-09-19T05:00:00'))
    inputs = fixture_inputs()
    result, body = audit.capture(**inputs)
    assert result['trade_plan']['ready'] and body['timing']['aligned']
    return inputs, body


def test_calendar_weekend_and_us_early_close():
    kr = audit.clock_context('kr', instant('2026-09-19T05:00:00'))
    assert kr['expected_price_session'] == '2026-09-18'
    assert kr['evaluation_sessions'][0]['date'] == '2026-09-21'
    # Thanksgiving is closed; Friday is a scheduled 13:00 ET close, before a weekend.
    us = audit.clock_context('us', instant('2026-11-27T18:01:00'))
    assert us['expected_price_session'] == '2026-11-27'
    assert us['evaluation_sessions'][0]['date'] == '2026-11-30'
    before = audit.clock_context('us', instant('2026-11-27T17:59:00'))
    assert before['expected_price_session'] == '2026-11-25'
    assert before['evaluation_sessions'][0]['date'] == '2026-11-30'
    assert before['quote_verified'] is False


def test_calendar_dst_and_out_of_range():
    c = audit.clock_context('us', instant('2026-03-06T22:00:00'))
    assert c['evaluation_sessions'][0]['open'] == '2026-03-09T13:30:00+00:00'
    assert not audit.clock_context('kr', instant('2099-01-01T00:00:00'))['ready']
    with pytest.raises(ValueError):
        audit.clock_context('kr', datetime(2026, 9, 19))


def test_replay_freezes_costs_and_mutable_inputs(captured, monkeypatch):
    inputs, body = captured
    inputs['prices']['005930'][-1] = 999
    inputs['profile']['cash'] = 0
    monkeypatch.setenv('PAPER_KR_SELL_TAX_BPS', '900')
    assert audit.replay(body)['matched'] is True
    assert body['inputs']['profile']['cash'] == 1000
    assert body['timing']['source_available_at_verified'] is False
    body['engine_version'] = 'old'
    assert audit.replay(body)['matched'] is None


def test_profile_save_timestamp_does_not_manufacture_new_evidence(captured):
    inputs, original = captured
    inputs['profile'].update(updated=100, configured=True)
    _, first = audit.capture(**inputs)
    inputs['profile']['updated'] = 200
    _, second = audit.capture(**inputs)
    assert audit.digest(first) == audit.digest(second) == audit.digest(original)


def test_rejected_buy_candidate_cannot_hide_future_input(captured):
    inputs, _ = captured
    inputs['universe'].append({'ticker': 'BAD', 'sector': None})
    inputs['signal_by_ticker']['BAD'] = SimpleNamespace(kind='BUY', score=9, event_risk=False)
    inputs['prices']['BAD'] = [100]
    inputs['dates_by']['BAD'] = ['2099-01-01']
    _, body = audit.capture(**inputs)
    assert not body['timing']['aligned'] and 'BAD' in body['timing']['invalid_tickers']


@pytest.mark.parametrize('mode', ['future', 'stale', 'weekend', 'bad_date', 'nan', 'duplicate'])
def test_bad_price_session_blocks_evidence(mode, captured):
    inputs, _ = captured
    ds, ps = inputs['dates_by']['005930'], inputs['prices']['005930']
    if mode == 'future': ds[-1] = '2026-09-21'
    elif mode == 'stale': ds.pop(); ps.pop()
    elif mode == 'weekend': ds[-2] = '2026-09-13'
    elif mode == 'bad_date': ds[-1] = 'not-a-date'
    elif mode == 'nan': ps[-1] = float('nan')
    elif mode == 'duplicate': ds[-1] = ds[-2]
    _, body = audit.capture(**inputs)
    assert body['timing']['aligned'] is False
    assert not cf.evaluate(body, prices={}, dates_by={})['ready']


def forward_inputs(inputs, body, count=4):
    ds = inputs['dates_by']['005930'] + [s['date'] for s in body['timing']['evaluation_sessions'][:count]]
    ps = inputs['prices']['005930'] + [200 + i * 10 for i in range(count)]
    return dict(prices={'005930': ps}, dates_by={'005930': ds})


def test_paired_nav_uses_next_session_and_frozen_costs(captured, monkeypatch):
    inputs, body = captured
    forward = forward_inputs(inputs, body)
    end = body['timing']['evaluation_sessions'][3]['close']
    now = datetime.fromisoformat(end).replace(hour=23)
    result = cf.evaluate(body, **forward, now=now)
    assert result['ready'] and result['completed_sessions'] == 4
    assert result['entry_date'] == '2026-09-21'
    assert result['metrics']['hold']['nav'] == 1000 + 10 * 230
    assert result['path'][0]['hold'] == 3000
    assert result['fills']['policy'][0]['reference_price'] == 200
    assert result['metrics']['policy']['nav'] < result['metrics']['hold']['nav']
    assert result['delta_vs_hold_pp'] == pytest.approx(result['metrics']['policy']['return_pct'] - result['metrics']['hold']['return_pct'])
    assert result['oos_verified'] is False and result['live_eligible'] is False
    monkeypatch.setenv('PAPER_KR_SELL_TAX_BPS', '900')
    assert cf.evaluate(body, **forward, now=now) == result


def test_future_prices_never_mark_unclosed_sessions(captured):
    inputs, body = captured
    forward = forward_inputs(inputs, body, 21)
    assert not cf.evaluate(body, **forward, now=instant('2026-09-19T08:00:00'))['ready']
    result = cf.evaluate(body, **forward, now=instant('2026-09-21T07:00:00'))
    assert result['ready'] and result['completed_sessions'] == 1
    assert list(result['observed_panel']['005930']) == ['2026-09-21']


def test_missing_session_is_not_skipped_and_revisions_block(captured):
    inputs, body = captured
    forward = forward_inputs(inputs, body)
    forward['dates_by']['005930'].pop(-2)
    forward['prices']['005930'].pop(-2)
    end = datetime.fromisoformat(body['timing']['evaluation_sessions'][3]['close']).replace(hour=23)
    result = cf.evaluate(body, **forward, now=end)
    assert not result['ready'] and '결손' in result['reason']
    forward = forward_inputs(inputs, body)
    forward['prices']['005930'][len(inputs['prices']['005930'])-1] /= 2
    assert '기준 가격' in cf.evaluate(body, **forward, now=end)['reason']


def test_gap_cannot_create_credit_or_hindsight_resize(captured):
    inputs, body = captured
    body = copy.deepcopy(body)
    body['result']['trade_plan']['instructions'] = [{'ticker': '005930', 'side': 'buy', 'qty': 100}]
    result = cf.evaluate(body, **forward_inputs(inputs, body), now=instant('2026-09-21T07:00:00'))
    assert not result['ready'] and result['path'] == []


def test_artifact_is_deduplicated_private_and_tamper_evident(tmp_path, monkeypatch, captured):
    monkeypatch.setattr(db, 'DB', tmp_path / 'audit.db')
    _, body = captured
    first = db.portfolio_artifact_add(1, 'kr', body)
    assert db.portfolio_artifact_add(1, 'kr', body) == first
    assert len(db.portfolio_artifact_list(1, 'kr')) == 1
    assert db.portfolio_artifact_get(2, 'kr', first['id']) is None
    assert db.portfolio_artifact_get(1, 'us', first['id']) is None
    assert audit.replay(db.portfolio_artifact_get(1, 'kr', first['id'])['body'])['matched']
    changed = copy.deepcopy(body)
    changed['inputs']['prices']['005930'][0] += 1
    assert db.portfolio_artifact_add(1, 'kr', changed)['id'] != first['id']
    c = db.conn()
    c.execute('UPDATE portfolio_decision_artifacts SET payload=? WHERE id=?',
              (zlib.compress(audit.canonical(changed)), first['id']))
    c.commit(); c.close()
    with pytest.raises(ValueError, match='integrity'):
        db.portfolio_artifact_get(1, 'kr', first['id'])


def test_daily_snapshots_are_atomic_without_deleting_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'snapshots.db')
    db.conn().close()
    def write(_):
        return db.portfolio_snapshot_add_once(1, 'kr', as_of='2026-09-18', source='daily_close',
                                              total_value=100, data_quality='complete', payload={})
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))
    assert len(set(ids)) == 1


def test_comparisons_are_append_only_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'compare.db')
    one = db.portfolio_comparison_add(1, 'kr', 'artifact', {'path': [1]})
    assert db.portfolio_comparison_add(1, 'kr', 'artifact', {'path': [1]}) == one
    two = db.portfolio_comparison_add(1, 'kr', 'artifact', {'path': [2]})
    assert two != one
    assert db.portfolio_comparison_latest(1, 'kr', 'artifact')['id'] == two['id']
    assert db.portfolio_comparison_latest(2, 'kr', 'artifact') is None
    db.portfolio_comparison_add(1, 'kr', 'artifact', {'path': [1]})
    assert db.portfolio_comparison_latest(1, 'kr', 'artifact')['id'] == one['id']
    c = db.conn()
    assert c.execute('SELECT COUNT(*) FROM portfolio_comparisons').fetchone()[0] == 2
    c.close()


def test_first_forward_prices_cannot_be_silently_revised(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'revision.db')
    first = {'ready': True, 'path': [100], 'observed_panel': {'A': {'2026-09-21': 100}}, 'metrics': {}}
    assert db.portfolio_comparison_add(1, 'kr', 'artifact', first)['result']['ready']
    changed = {'ready': True, 'path': [110], 'observed_panel': {'A': {'2026-09-21': 110}}, 'metrics': {}}
    result = db.portfolio_comparison_add(1, 'kr', 'artifact', changed)['result']
    assert not result['ready'] and result['path'] == [] and 'metrics' not in result
    assert result['price_revisions'] == [{'ticker': 'A', 'date': '2026-09-21'}]
    # No mutation of the caller, and another attempt is still blocked against the original.
    assert changed['ready']
    assert not db.portfolio_comparison_add(1, 'kr', 'artifact', changed)['result']['ready']
    c = db.conn()
    assert c.execute('SELECT price FROM portfolio_forward_prices').fetchone()[0] == 100
    assert c.execute('SELECT COUNT(*) FROM portfolio_comparisons').fetchone()[0] == 2
    c.close()


@pytest.mark.parametrize('market', ['kr', 'us'])
def test_close_bundle_never_splices_live_provisional_quotes(market, monkeypatch):
    from signal_desk import store
    prices, dates = {'A': [100, 101]}, {'A': ['2026-09-17', '2026-09-18']}
    monkeypatch.setattr(store, '_LIVE_QUOTES', {'A': 999})
    monkeypatch.setattr(store, '_kr_prices_raw', lambda: (prices, dates))
    monkeypatch.setattr(store, '_us_prices_raw', lambda: (prices, {}, dates))
    actual, days = store.load_portfolio_close_bundle(market)
    assert actual == prices and days == dates
    actual['A'][-1] = 777
    assert prices['A'][-1] == 101
