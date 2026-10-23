import numpy as np
import pytest

from scripts.measure import kr_net_payoff_target as lab


def test_gain_loss_identity_includes_zero_losing_cases_and_cost():
    gross = np.array([-.15, 0, .01, .1])
    g, l = lab.targets(gross)
    np.testing.assert_allclose((g-l)/100, lab.net(gross))
    assert g[0] == 0 and l[-1] == 0 and lab.net(0) < 0


def test_selection_never_promotes_rank_seven_and_negative_is_valid():
    scores = {str(i): -i for i in range(8)}
    assert lab.chosen(scores, 'two_head_net') == []
    assert lab.chosen(scores, 'momentum12_skip21') == [str(i) for i in range(6)]


def test_new_cohort_preserves_expiry_identity_attempts_and_deadline():
    old = [{'side': 'sell', 'ticker': 'A', 'attempts': 4, 'deadline': '2024-12-30'}]
    new = [{'side': 'buy', 'ticker': 'B'}, {'side': 'sell', 'ticker': 'A', 'attempts': 0}]
    assert lab.keep_expiries(old, new) == old + new[:1]


def test_incomplete_phase_cannot_establish_mean_failure():
    groups = []
    for s in lab.SCENARIOS:
        for p in range(5):
            groups.append({'scenario': s, 'phase': p, 'results': {a: {
                'complete': p != 0, 'net_return_pct': -1 if p else None,
                'price_mdd_pct': -2, 'pnl_by_ticker': {}}
                for a in (*lab.ARMS, 'momentum_same_slots')}})
    assert lab.grade(groups)['decision'] == 'inconclusive_data'
    groups[0]['results']['two_head_net'].update(complete=True, net_return_pct=-1)
    assert lab.grade(groups)['decision'] == 'rejected'


def synthetic_replay(failed_first_exit=False, scenario='75bp_full'):
    days = lab.f.calendar('2024-09-30', '2024-12-31')
    rows = {d: {'000010': {'LIST_SHRS': 100000000, 'TDD_OPNPRC': 100,
        'TDD_CLSPRC': 100, 'ACC_TRDVOL': 100000000}} for d in days}
    dates = lab.f.calendar('2024-10-02', '2024-10-16')
    if failed_first_exit:
        rows[lab.market_clock.next_sessions('kr', dates[0], 6)[-1]]['000010']['TDD_OPNPRC'] = 0
    inputs = lab.f.Inputs({}, {}, rows, {d: days[i-1] for i,d in enumerate(days) if i}, {}, {})
    predictions = {d: {a: {'000010': .1} for a in lab.ARMS} for d in dates}
    cfg = {'scenarios': list(lab.SCENARIOS), 'buy_limit_above_close': .01,
           'prior_volume_participation': .01, 'sell_residual_attempts': 5}
    return lab.replay(inputs, predictions, cfg, scenario, 0)


def test_same_name_roundtrip_rebuys_only_after_full_expiry_sell():
    result = synthetic_replay()['results']['two_head_net']
    assert result['complete']
    assert [f['side'] for f in result['fills']] == ['buy', 'sell', 'buy', 'sell']
    assert result['fills'][1]['on'] == result['fills'][2]['on']
    assert result['net_return_pct'] < 0 and result['fees_krw'] > 0


def test_failed_expiry_cancels_new_same_name_buy_without_resetting_retry():
    result = synthetic_replay(True)['results']['two_head_net']
    assert result['complete']
    assert [f['side'] for f in result['fills']] == ['buy', 'sell']
    assert any(e['state'] == 'sell_retry' for e in result['events'])


def test_delayed_same_name_buy_survives_until_original_due_on():
    result = synthetic_replay(scenario='75bp_delay1')['results']['two_head_net']
    assert result['complete']
    assert [f['side'] for f in result['fills']] == ['buy', 'sell', 'buy', 'sell']
    assert result['fills'][1]['on'] == result['fills'][2]['on']


def test_complete_drawdown_counterexample_survives_other_missing_phases():
    r = {'complete': True, 'net_return_pct': 1, 'price_mdd_pct': -15}
    g = {'scenario': '75bp_full', 'phase': 0,
         'results': {a: dict(r) for a in (*lab.ARMS, 'momentum_same_slots')}}
    assert lab.grade([g])['decision'] == 'rejected'


def test_fit_purges_future_labels_and_heads_predict_in_return_units():
    pytest.importorskip('sklearn')
    from threadpoolctl import threadpool_limits
    plan = {'model': dict(learning_rate=.05, max_iter=50, max_depth=2,
        max_leaf_nodes=4, min_samples_leaf=200, max_bins=32,
        l2_regularization=1., early_stopping=False, random_state=20261023)}
    cfg = {'train_start': '2024-04-01', 'features': [str(i) for i in range(14)],
           'standardized_clip': 8, 'alpha_weighted_mean_objective': .1}
    rows = [{'date': '2024-04-01', 'exit_date': '2024-04-10', 'ticker': str(i),
        'state': 'matured', 'features': [float(i % 3)]*14, 'target': .04 if i%2 else -.03}
        for i in range(400)]
    future = {**rows[0], 'date': '2024-04-12', 'exit_date': '2024-04-25', 'target': 1000}
    with threadpool_limits(limits=1):
        model = lab.fit(rows+[future], cfg, '2024-04-20', plan)
        scores, heads = lab.predict(rows, model)
    assert model['ridge']['training_rows'] == 400
    assert np.all(heads[0] > 0) and np.all(heads[1] > 0)
    np.testing.assert_allclose(list(scores['two_head_net'].values()), heads[0]-heads[1])
    assert np.max(heads[0]) < .04


def test_known_drawdown_comparator_failure_survives_missing_candidate():
    groups = []
    for p in range(5):
        rs = {a: {'complete': True, 'net_return_pct': 1, 'price_mdd_pct': -5}
              for a in (*lab.ARMS, 'momentum_same_slots')}
        rs['two_head_net'].update(complete=p==0, price_mdd_pct=-8 if p==0 else None)
        groups.append({'scenario': '75bp_full', 'phase': p, 'results': rs})
    assert lab.grade(groups)['decision'] == 'rejected'
