from copy import deepcopy

import pytest

from scripts.measure import kr_current_momentum as lab
from tests.test_kr_risk_peer_momentum import rows


def test_latest_close_changes_rank_without_changing_groups():
    rs = rows()
    prices = {r['ticker']: [100.]*253 for r in rs}
    rank, groups, *_ = lab.selection(rs, prices)
    assert sorted(rank, key=rank.get, reverse=True)[:2] == ['000000', '000001']
    prices['000002'][-1] = 110.
    rank2, groups2, scores, _ = lab.selection(rs, prices)
    assert groups == groups2
    assert rank2['000002'] == 0 and scores['000002'] == pytest.approx(.1)
    assert rs[2]['features'][4] == 2.
    changed = deepcopy(rs)
    for r in changed:
        r.update(target=999., state='missing_entry_or_exit_open')
    assert lab.selection(changed[::-1], prices) == lab.selection(rs, prices)
    prices['000002'][0] = 0.
    with pytest.raises(ValueError, match='invalid_price_window'):
        lab.selection(rs, prices)


def matrices():
    folds, controls = [], []
    for month in ('06', '07', '08', '09'):
        start = f'2024-{month}-01'
        folds.append({'start': start, 'paths': [dict(arm=lab.ARM, cost_bps=c, phase=p,
            complete=True, net_cohort_return_pct=2.) for c in (75, 120) for p in range(5)]})
        controls.append({'start': start, 'paths': [dict(arm=a, cost_bps=c, phase=p,
            complete=True, net_cohort_return_pct=1.) for a in lab.parent.ARMS
            for c in (75, 120) for p in range(5)]})
    return folds, controls


def test_verdict_failure_missing_priority_and_robustness():
    fs, cs = matrices()
    assert lab.verdict(fs, cs)['decision'] == 'development_mechanism_support'
    cs[0]['paths'][-1].update(complete=False, net_cohort_return_pct=None)
    assert lab.verdict(fs, cs)['decision'] == 'development_mechanism_support'
    fs[0]['paths'][0].update(complete=False, net_cohort_return_pct=None)
    assert lab.verdict(fs, cs)['decision'] == 'inconclusive_mechanism'
    for p in fs[1]['paths']:
        p['net_cohort_return_pct'] = -1.
    assert lab.verdict(fs, cs)['decision'] == 'rejected_mechanism'
    fs, cs = matrices()
    for p in fs[2]['paths']:
        p['net_cohort_return_pct'] = .5
    assert lab.verdict(fs, cs)['decision'] == 'rejected_robustness'
    fs[0]['paths'].pop()
    with pytest.raises(ValueError, match='invalid_candidate_matrix'):
        lab.verdict(fs, cs)
