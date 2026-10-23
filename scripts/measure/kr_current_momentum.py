"""Frozen latest-close momentum experiment using preserved control paths."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean
import subprocess
import sys
import time

from scripts.measure import kr_risk_peer_momentum as parent

old, temporal, frozen = parent.old, parent.temporal, parent.frozen
PLAN_COMMIT = 'eed50bf'
PLAN = 'docs/kr-current-momentum-design-2026-10-23.json'
PROSE = 'docs/KR-JULY-LOSS-CURRENT-MOMENTUM-DESIGN-2026-10-23.md'
ARM = 'current_momentum'
CODE = tuple(dict.fromkeys(('scripts/measure/kr_current_momentum.py',
    'tests/test_kr_current_momentum.py', *parent.CODE)))


def selection(rows, prices):
    updated = []
    scores = {}
    for row in rows:
        closes = prices[row['ticker']]
        if len(closes) != 253 or any(not math.isfinite(v) or v <= 0 for v in closes):
            raise ValueError('invalid_price_window')
        score = closes[-1]/closes[0]-1
        values = list(row['features'])
        values[4] = score
        scores[row['ticker']] = score
        updated.append({**row, 'features': values})
    ranks, groups, diagnostics = parent.selection(updated)
    return ranks['risk_peer_momentum'], groups, scores, diagnostics['risk_peer_momentum']


def verdict(folds, controls):
    expected = {(c, p) for c in (75, 120) for p in range(5)}
    def average(paths, arm, cost):
        ps = [p for p in paths if p['arm'] == arm and p['cost_bps'] == cost]
        if len(ps) != 5 or {p['phase'] for p in ps} != set(range(5)):
            raise ValueError('invalid_path_matrix')
        return mean(p['net_cohort_return_pct'] for p in ps) if all(p['complete'] for p in ps) else None
    if len(folds) != 4 or [f['start'] for f in folds] != [f['start'] for f in controls]:
        raise ValueError('invalid_fold_matrix')
    summaries, failures, unknown = [], [], []
    for i, (fold, control) in enumerate(zip(folds, controls)):
        if len(fold['paths']) != 10 or {(p['cost_bps'], p['phase']) for p in fold['paths']} != expected:
            raise ValueError('invalid_candidate_matrix')
        for cost in (75, 120):
            v = average(fold['paths'], ARM, cost)
            means = {a: average(control['paths'], a, cost) for a in parent.ARMS}
            summaries.append({'month': fold['start'][:7], 'cost_bps': cost,
                              'candidate': v, 'controls': means})
            key = f'{fold["start"][:7]}:{cost}'
            if i < 2:
                if v is None or means['risk_peer_momentum'] is None:
                    unknown.append(key)
                if v is not None:
                    if v <= 0:
                        failures.append(key+':nonpositive')
                    for a, m in means.items():
                        if m is not None and v <= m:
                            failures.append(key+':not_above:'+a)
    if failures:
        decision = 'rejected_mechanism'
    elif unknown:
        decision = 'inconclusive_mechanism'
    else:
        for s in summaries[4:]:
            v, m = s['candidate'], s['controls']['risk_peer_momentum']
            if v is not None and (v <= 0 or (m is not None and v <= m)):
                failures.append(f'{s["month"]}:{s["cost_bps"]}:robustness')
        decision = 'rejected_robustness' if failures else 'development_mechanism_support'
    return {'decision': decision, 'means': summaries, 'violations': failures,
            'required_unknown': unknown, 'adopted': False}


def attribution(records, cost):
    groups, tickers, exits = {}, {}, {}
    for phase in range(5):
        if any(r[ARM]['missing'] for r in records[phase::5]):
            return None
        wealth = 1.
        for r in records[phase::5]:
            ys = r[ARM]['targets']
            nets = [(1+y)*(1-cost/20000)/(1+cost/20000)-1 for y in ys]
            for t, n in zip(r[ARM]['selected'], nets):
                contribution = wealth*n/6/5*100
                g = str(r['groups'][t])
                groups[g] = groups.get(g, 0.)+contribution
                tickers[t] = tickers.get(t, 0.)+contribution
                month = r['exit_date'][:7]
                exits[month] = exits.get(month, 0.)+contribution
            wealth *= 1+mean(nets)
    return {'group_pp': groups, 'ticker_pp': tickers, 'exit_month_pp': exits}


def run(root, commit):
    if root != old.ROOT.resolve() or Path(__file__).resolve() != root/CODE[0]:
        raise ValueError('wrong_repository_or_runner')
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
    previous = json.loads(frozen.fixed_bytes(root/plan['parent_result'], plan['parent_result_sha256']))
    scope = json.loads(frozen.fixed_bytes(old.SCOPE, plan['source_scope_sha256']))
    frozen.fixed_bytes(old.FINANCIAL, plan['financial_sha256'])
    days, sources = old.load_krx_archives([root/'data/research'/r['archive'] for r in scope['sources'][:9]])
    if sources != scope['sources'][:9] or sources != previous['sources']:
        raise ValueError('source_drift')
    panel = old.PricePanel(days)
    by_day = {d: {r['ISU_CD']: r for r in rs} for d, rs in days.items()}
    financials = old.load_financials(old.FINANCIAL)['records']
    roster = {r['ISU_CD'] for r in old.top200(days[old.plan()['financial_roster_asof']])}
    folds = []
    for (lo, hi), control in zip(plan['cohort_windows'], previous['folds']):
        records = []
        if (lo, hi) != (control['start'], control['end']):
            raise ValueError('control_window_drift')
        expected_days = [d for d in panel.sessions if lo <= d <= hi]
        if expected_days != [r['date'] for r in control['records']]:
            raise ValueError('control_dates_drift')
        for day, prior in zip(expected_days, control['records']):
            rows, prices, *_ = old.features_at(day, panel, by_day, financials, roster)
            old_ranks, old_groups, _ = parent.selection(rows)
            if old_groups != prior['groups'] or old_ranks != prior['rankings']:
                raise ValueError('control_inputs_drift')
            ranks, groups, scores, diag = selection(rows, prices)
            labeled = [{**r, **old.label_at(day, r['ticker'], panel, by_day, 5)} for r in rows]
            cohort = temporal.cohort(labeled, ranks)
            selected = set(cohort['selected'])
            records.append({'date': day, 'groups': groups, 'rankings': ranks,
                'diagnostics': diag, ARM: cohort, 'common_missing': bool(cohort['missing']),
                'exit_date': panel.sessions[panel.index[day]+6],
                'changed_slots': len(selected-set(prior['risk_peer_momentum']['selected'])),
                'selected_inputs': [{ 'ticker': r['ticker'], 'current_score': scores[r['ticker']],
                    'parent_score': r['features'][4],
                    **old.label_at(day, r['ticker'], panel, by_day, 5)}
                    for r in rows if r['ticker'] in selected | set(prior['risk_peer_momentum']['selected'])]})
        paths = [temporal.path(records, ARM, c, p) for c in plan['cost_bps'] for p in range(5)]
        folds.append({'start': lo, 'end': hi, 'records': records, 'paths': paths,
                      'attribution': {str(c): attribution(records, c) for c in plan['cost_bps']}})
    return {'candidate': plan['candidate'], 'implementation_commit': commit, 'provenance': provenance,
        'parent_result_sha256': plan['parent_result_sha256'], 'sources': sources,
        'folds': folds, 'verdict': verdict(folds, previous['folds']), 'cohort_paths': 40,
        'complete_cohort_paths': sum(p['complete'] for f in folds for p in f['paths']),
        'reused_control_paths': 160, 'account_paths_executed': 0, 'fit_calls': 0,
        'new_source_requests': 0, 'historical_runs': 1, 'elapsed_seconds': time.monotonic()-started,
        'production_changed': False, 'pristine_oos': False, 'verified_total_return': False,
        'integrated_roadmap_percent': 40}


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
    print(json.dumps({'verdict': result['verdict'], 'complete': result['complete_cohort_paths']}))


if __name__ == '__main__':
    main()
