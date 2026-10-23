from copy import deepcopy

import pytest

from scripts.measure import kr_risk_peer_momentum as lab
from scripts.measure import kr_temporal_selection as temporal


def rows(n=161):
    return [{'ticker': f'{i:06}', 'features': [0., 0., 0., 0., float(i), i/1000],
             'market_cap': 1000+i} for i in range(n)]


def test_uneven_groups_exact_capacity_and_execution_priority():
    ranks, groups, _ = lab.selection(rows())
    assert [sum(g == i for g in groups.values()) for i in range(3)] == [53, 54, 54]
    selected = sorted(ranks['risk_peer_momentum'], key=ranks['risk_peer_momentum'].get, reverse=True)[:6]
    assert selected == ['000160', '000159', '000106', '000105', '000052', '000051']
    assert [sum(groups[t] == i for t in selected) for i in range(3)] == [2, 2, 2]


def test_labels_and_input_order_cannot_change_tied_selection():
    rs = rows(160)
    for r in rs:
        r['features'][4:6] = [1., .01]
        r['market_cap'] = 1000
    baseline = lab.selection(rs)
    changed = deepcopy(rs)
    for r in changed:
        r.update(state='missing_entry_or_exit_open', target=999.)
    assert lab.selection(changed[::-1]) == baseline
    ranks, groups, _ = baseline
    selected = sorted(ranks['risk_peer_momentum'], key=ranks['risk_peer_momentum'].get, reverse=True)[:6]
    assert selected == ['000000', '000001', '000053', '000054', '000106', '000107']
    labeled = [{**r, 'state': 'matured', 'target': .02} for r in rs]
    labeled[0]['state'] = 'share_history_unverified'
    cohort = temporal.cohort(labeled, ranks['risk_peer_momentum'])
    assert '000000' in cohort['selected'] and cohort['gross'] is None


def test_invalid_features_are_not_silently_ranked():
    rs = rows()
    rs[0]['features'][5] = float('nan')
    with pytest.raises(ValueError, match='invalid_selection_input'):
        lab.selection(rs)


def fold():
    return {'start': '2024-06-03', 'paths': [
        {'arm': a, 'cost_bps': c, 'phase': p, 'complete': True,
         'net_cohort_return_pct': 2. if a == lab.ARMS[0] else 1.}
        for a in lab.ARMS for c in (75, 120) for p in range(5)]}


def test_gate_requires_both_costs_all_controls_and_all_phases():
    f = fold()
    assert lab.selection_gate([f])['decision'] == 'passed_selection'
    f['paths'][-1]['net_cohort_return_pct'] = 6.
    assert lab.selection_gate([f])['decision'] == 'rejected_selection'
    f['paths'][-1]['complete'] = False
    f['paths'][-1]['net_cohort_return_pct'] = None
    assert lab.selection_gate([f])['decision'] == 'inconclusive_selection'
    f['paths'].pop()
    with pytest.raises(ValueError, match='invalid_june_path_matrix'):
        lab.selection_gate([f])
