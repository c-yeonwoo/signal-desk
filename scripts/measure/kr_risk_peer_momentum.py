"""Frozen offline risk-peer selection screen; no production imports this runner."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean
import subprocess
import sys
import time

from scripts.measure import kr_ridge_selection_lab as old
from scripts.measure import kr_temporal_selection as temporal
from scripts.measure import kr_frozen_forecast_policy as frozen

PLAN_COMMIT = '663d52f0ca58931dbefb5873c646de0648d71bcd'
PLAN = 'docs/kr-risk-peer-momentum-design-2026-10-23.json'
PROSE = 'docs/KR-RISK-PEER-MOMENTUM-DESIGN-2026-10-23.md'
ARMS = ('risk_peer_momentum', 'momentum12_skip21', 'low_volatility', 'risk_peer_cap')
CODE = tuple(dict.fromkeys(('scripts/measure/kr_risk_peer_momentum.py',
    'tests/test_kr_risk_peer_momentum.py', *temporal.CODE)))


def selection(rows):
    """Uses only contemporaneous features; labels never enter group/rank decisions."""
    if len(rows) < 160 or len({r['ticker'] for r in rows}) != len(rows):
        raise ValueError('invalid_feature_universe')
    if any(not all(isinstance(v, (int, float)) and math.isfinite(v)
                   for v in (r['features'][4], r['features'][5], r['market_cap']))
           or r['features'][5] < 0 or r['market_cap'] <= 0 for r in rows):
        raise ValueError('invalid_selection_input')
    ordered = sorted(rows, key=lambda r: (r['features'][5], r['ticker']))
    n = len(ordered)
    groups = [ordered[:n//3], ordered[n//3:2*n//3], ordered[2*n//3:]]
    mom = lambda r: (-r['features'][4], r['ticker'])
    cap = lambda r: (-r['market_cap'], r['ticker'])
    selected = {
        'risk_peer_momentum': sorted([r for g in groups for r in sorted(g, key=mom)[:2]], key=mom),
        'momentum12_skip21': sorted(rows, key=mom)[:6],
        'low_volatility': ordered[:6],
        'risk_peer_cap': sorted([r for g in groups for r in sorted(g, key=cap)[:2]], key=cap),
    }
    keys = {'risk_peer_momentum': mom, 'momentum12_skip21': mom,
            'low_volatility': lambda r: (r['features'][5], r['ticker']), 'risk_peer_cap': cap}
    rankings = {}
    diagnostics = {}
    def percentile(row, feature):
        v = row['features'][feature] if feature != 'cap' else row['market_cap']
        xs = [r['features'][feature] if feature != 'cap' else r['market_cap'] for r in rows]
        return (sum(x < v for x in xs)+sum(x == v for x in xs)/2)/n
    for arm, chosen in selected.items():
        ids = {r['ticker'] for r in chosen}
        ranking = chosen + sorted([r for r in rows if r['ticker'] not in ids], key=keys[arm])
        rankings[arm] = {r['ticker']: -i for i, r in enumerate(ranking)}
        vols = [r['features'][5] for r in chosen]
        diagnostics[arm] = {'volatility_mean': mean(vols), 'volatility_min': min(vols),
            'volatility_max': max(vols), 'momentum_percentile': mean(percentile(r, 4) for r in chosen),
            'cap_percentile': mean(percentile(r, 'cap') for r in chosen)}
    return rankings, {r['ticker']: i for i, g in enumerate(groups) for r in g}, diagnostics


def selection_gate(folds):
    june = [f for f in folds if f['start'] == '2024-06-03']
    if len(june) != 1:
        raise ValueError('missing_or_duplicate_june')
    paths = june[0]['paths']
    expected = {(a, c, p) for a in ARMS for c in (75, 120) for p in range(5)}
    keys = [(p['arm'], p['cost_bps'], p['phase']) for p in paths]
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError('invalid_june_path_matrix')
    if not all(p['complete'] for p in paths):
        return {'decision': 'inconclusive_selection', 'means': {}, 'violations': []}
    means = {str(c): {a: mean(p['net_cohort_return_pct'] for p in paths
        if p['arm'] == a and p['cost_bps'] == c) for a in ARMS} for c in (75, 120)}
    violations = [f'{c}:not_above:{a}' for c, vals in means.items() for a in ARMS[1:]
                  if vals[ARMS[0]] <= vals[a]]
    return {'decision': 'rejected_selection' if violations else 'passed_selection',
            'means': means, 'violations': violations}


def run(root, commit):
    if root != old.ROOT.resolve():
        raise ValueError('wrong_repository')
    started = time.monotonic()
    provenance = {}
    for name, revision in [(PLAN, PLAN_COMMIT), (PROSE, PLAN_COMMIT), *[(n, commit) for n in CODE]]:
        body = (root/name).read_bytes()
        if body != subprocess.check_output(['git', 'show', f'{revision}:{name}'], cwd=root):
            raise ValueError('uncommitted_or_changed_input:'+name)
        provenance[name] = frozen.sha(body)
        if name.endswith('.py'):
            module = sys.modules.get(name.removeprefix('src/').removesuffix('.py').replace('/', '.'))
            if module is not None and Path(module.__file__).resolve() != root/name:
                raise ValueError('import_outside_repository:'+name)
    plan = json.loads((root/PLAN).read_bytes())
    cfg = old.plan()
    scope = json.loads(frozen.fixed_bytes(old.SCOPE, plan['source_scope_sha256']))
    frozen.fixed_bytes(old.FINANCIAL, plan['financial_sha256'])
    days, sources = old.load_krx_archives([root/'data/research'/r['archive'] for r in scope['sources'][:9]])
    if sources != scope['sources'][:9]:
        raise ValueError('source_drift')
    panel = old.PricePanel(days)
    by_day = {d: {r['ISU_CD']: r for r in rs} for d, rs in days.items()}
    financials = old.load_financials(old.FINANCIAL)['records']
    roster = {r['ISU_CD'] for r in old.top200(days[cfg['financial_roster_asof']])}
    folds = []
    for lo, hi in plan['cohort_windows']:
        records = []
        for day in panel.sessions:
            if not lo <= day <= hi:
                continue
            rows, *_ = old.features_at(day, panel, by_day, financials, roster)
            ranks, groups, diagnostics = selection(rows)
            labeled = [{**r, **old.label_at(day, r['ticker'], panel, by_day, 5)} for r in rows]
            records.append({'date': day, 'groups': groups, 'rankings': ranks,
                'diagnostics': diagnostics,
                **{a: temporal.cohort(labeled, ranks[a]) for a in ARMS}})
        paths = [temporal.path([{**r, 'common_missing': bool(r[a]['missing'])} for r in records], a, c, p)
                 for a in ARMS for c in (75, 120) for p in range(5)]
        folds.append({'start': lo, 'end': hi, 'records': records, 'paths': paths})
    gate = selection_gate(folds)
    return {'candidate': plan['candidate'], 'implementation_commit': commit, 'provenance': provenance,
        'sources': sources, 'folds': folds, 'selection_gate': gate,
        'cohort_paths': sum(len(f['paths']) for f in folds),
        'complete_cohort_paths': sum(p['complete'] for f in folds for p in f['paths']),
        'account_status': 'pending_source_preflight' if gate['decision'] == 'passed_selection' else 'not_run_selection_gate',
        'account_paths_executed': 0, 'fit_calls': 0, 'new_source_requests': 0,
        'historical_runs': 1, 'elapsed_seconds': time.monotonic()-started,
        'pristine_oos': False, 'verified_total_return': False,
        'production_changed': False, 'integrated_roadmap_percent': 40}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--implementation-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if not output.is_relative_to(root/'data/research') or output.exists():
        parser.error('new research output required')
    result = run(root, args.implementation_commit)
    with output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({k: result[k] for k in ('selection_gate', 'complete_cohort_paths', 'elapsed_seconds')}))


if __name__ == '__main__':
    main()
