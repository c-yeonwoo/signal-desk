"""Frozen, offline two-head net payoff experiment. Never imported by production."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import pickle
import resource
import subprocess
import sys
import time

import numpy as np

from scripts.measure import kr_frozen_forecast_policy as f
from scripts.measure import kr_ridge_selection_lab as old
from scripts.measure import kr_temporal_selection as temporal
from signal_desk import market_clock
from signal_desk.signals import research_ridge as ridge
from signal_desk.signals.research_net_alpha_execution import ExecutionBook

PLAN = 'docs/kr-net-payoff-target-design-2026-10-23.json'
PLAN_COMMIT = 'c04fadc3a6a492007c0e5e1fb8e171ff3d8ca2d3'
PROSE = 'docs/KR-NET-PAYOFF-TARGET-DESIGN-2026-10-23.md'
ARMS = ('two_head_net', 'single_net_tree', 'ridge14', 'momentum12_skip21')
SCENARIOS = ('75bp_full', '120bp_full', '75bp_half', '75bp_delay1')
CODE = tuple(dict.fromkeys(('scripts/measure/kr_net_payoff_target.py', 'tests/test_kr_net_payoff_target.py',
        'scripts/measure/requirements-net-payoff.txt',
        'scripts/measure/kr_temporal_selection.py', 'scripts/measure/kr_ridge_selection_lab.py',
        'scripts/measure/kr_frozen_forecast_policy.py',
        'src/signal_desk/signals/research_ridge.py',
        'src/signal_desk/signals/research_net_alpha.py',
        'src/signal_desk/signals/research_net_alpha_execution.py',
        'src/signal_desk/market_clock.py', *temporal.CODE)))


def net(gross, side=.006):
    return (1+np.asarray(gross))*(1-side)/(1+side)-1


def targets(gross):
    n = net(gross)
    return 100*np.maximum(n, 0), 100*np.maximum(-n, 0)


def fit(rows, cfg, cutoff, plan):
    from sklearn.ensemble import HistGradientBoostingRegressor
    selected = temporal.training(rows, cutoff)
    fit_cfg = {**cfg, 'train_end': max(r['date'] for r in selected),
               'fit_through': max(r['exit_date'] for r in selected), 'evaluation_start': cutoff}
    original = ridge.fit(selected, fit_cfg)
    x = ridge.transform([r['features'] for r in selected], original)[:, 1:]
    counts = Counter(r['date'] for r in selected)
    weights = np.asarray([len(selected)/(len(counts)*counts[r['date']]) for r in selected])
    params = {k: plan['model'][k] for k in ('learning_rate', 'max_iter', 'max_depth',
        'max_leaf_nodes', 'min_samples_leaf', 'max_bins', 'l2_regularization',
        'early_stopping', 'random_state')}
    heads, calls = [], 1
    for y in targets([r['target'] for r in selected]):
        model = None
        if np.any(y):
            model = HistGradientBoostingRegressor(loss='poisson', **params).fit(x, y, sample_weight=weights)
            calls += 1
        heads.append(model)
    single = HistGradientBoostingRegressor(loss='squared_error', **params).fit(
        x, 100*net([r['target'] for r in selected]), sample_weight=weights)
    return {'ridge': original, 'heads': heads, 'single': single, 'fit_calls': calls+1,
            'training_sha': old.encoded_digest(selected), 'params': params}


def predict(rows, model):
    x = ridge.transform([r['features'] for r in rows], model['ridge'])[:, 1:]
    heads = [np.zeros(len(x)) if h is None else h.predict(x)/100 for h in model['heads']]
    if any(not np.isfinite(h).all() or np.any(h < 0) for h in heads):
        raise ValueError('invalid_gain_loss_prediction')
    scores = {'two_head_net': heads[0]-heads[1], 'single_net_tree': model['single'].predict(x)/100,
              'ridge14': net(ridge.predict([r['features'] for r in rows], model['ridge'])),
              'momentum12_skip21': np.asarray([r['features'][4] for r in rows])}
    if any(not np.isfinite(v).all() for v in scores.values()):
        raise ValueError('nonfinite_score')
    return {a: dict(zip([r['ticker'] for r in rows], map(float, s))) for a, s in scores.items()}, heads


def chosen(scores, arm):
    top = sorted(scores, key=lambda t: (-scores[t], t))[:6]
    return top if arm.startswith('momentum') else [t for t in top if scores[t] > 0]


def keep_expiries(old_pending, new_pending):
    """A new close decision never replaces/restarts a previous exit."""
    return old_pending + [o for o in new_pending if o['side'] == 'buy']


def replay(inputs, predictions, cfg, scenario, phase):
    dates = sorted(predictions)
    scheduled = set(dates[phase::5])
    terminal = market_clock.next_sessions('kr', max(scheduled), 5)[-1]
    names = (*ARMS, 'momentum_same_slots')
    spec = {**cfg, 'evaluation_start': dates[0], 'evaluation_end': dates[-1],
            'liquidation_through': '2025-01-03', 'max_positions': 6,
            'initial_cash_krw': 10_000_000, 'decision_one_way_cost': .006}
    books = {a: ExecutionBook(spec, scenario) for a in names}
    curves = {a: [] for a in names}
    stop, trace = None, []
    for day in f.calendar(dates[0], '2024-12-31'):
        try:
            active = set().union(*(set(b.holdings) | {o['ticker'] for o in b.pending} for b in books.values()))
            f.unit_audit(inputs, day, active)
            opens = {t: {'price': f.field(inputs, day, t, 'TDD_OPNPRC', allow_zero=True),
                'open_tradable': True, 'source_id': f'raw:{day}:{t}'} for t in active}
            for book in books.values():
                # Sell first. A failed previous expiry cancels only this open's new same-name buy.
                buys = [o for o in book.pending if o['side'] == 'buy']
                book.pending = [o for o in book.pending if o['side'] == 'sell']
                for o in book.pending:
                    if day > o['deadline']:
                        raise f.DataGap(day, o['ticker'], 'expiry_retry_deadline')
                book.open_session(day, opens)
                if buys:
                    # Reuse the kernel's buy/cash/position checks at the same open, without settling twice.
                    sells = book.pending
                    book.pending = [o for o in buys if day < o['due_on'] or o['ticker'] not in book.holdings]
                    book.last_open = inputs.previous[day]
                    book.open_session(day, opens)
                    book.pending = keep_expiries(sells, book.pending)
                f.reconcile(book, day, 10_000_000)
            owned = set().union(*(set(b.holdings) for b in books.values()))
            closes = {t: f.field(inputs, day, t, 'TDD_CLSPRC') for t in active | owned}
            for a, b in books.items():
                nav = b.price_nav(closes)
                curves[a].append({'session': day, 'nav': nav,
                    'exposure': sum(q*closes[t] for t, q in b.holdings.items())/nav})
            if day not in scheduled and day != terminal:
                continue
            selections = {a: chosen(predictions[day][a], a) for a in ARMS} if day in scheduled else {a: [] for a in ARMS}
            k = len(selections['two_head_net'])
            selections['momentum_same_slots'] = selections['momentum12_skip21'][:k]
            trace.append({'date': day, 'selected': selections})
            for a, b in books.items():
                all_names = set(selections[a]) | set(b.holdings)
                cs = {t: f.field(inputs, day, t, 'TDD_CLSPRC') for t in all_names}
                vs = {t: f.field(inputs, day, t, 'ACC_TRDVOL', allow_zero=True) for t in all_names}
                pending = deepcopy(b.pending)
                expiring = {o['ticker'] for o in pending}
                fresh = set(b.holdings)-expiring
                # Only the previous cohort can be held at a new five-session close.
                for t in fresh:
                    if int(vs[t]*.01) < b.holdings[t]:
                        raise f.DataGap(day, t, 'expiry_volume_capped_unsubmitted_residual')
                if fresh:
                    # Create original expiry at this close; never overwrite old residuals.
                    b.submit_exits(day, fresh, cs, vs, input_id=f'{a}/{phase}/{day}/expiry')
                    for o in b.pending:
                        if o['ticker'] in fresh:
                            o['original_due_on'] = o['due_on']
                            o['deadline'] = market_clock.next_sessions('kr', o['due_on'], 5)[-1]
                    pending = deepcopy(b.pending)
                    # submit below needs the same close; restore the previous close marker only.
                    b.last_decision = None
                if day in scheduled:
                    priority = {t: -i for i, t in enumerate(selections[a])}
                    nav = b.price_nav(cs)
                    # New cohort buys are computed as a flat portfolio, still using actual own NAV.
                    held = b.holdings
                    b.holdings = {}
                    b.pending = []
                    b.submit(day, {t: 1/6 for t in selections[a]}, cs, vs, nav,
                             priority=priority, input_id=f'{a}/{phase}/{day}/entry', budget=1)
                    b.holdings = held
                    b.pending = keep_expiries(pending, b.pending)
        except f.DataGap as exc:
            stop = exc.detail
            break
    for b in books.values():
        if not stop and not b.holdings and not b.pending:
            # Price-free settlement of confirmed cash only, after the last available quote.
            for due in sorted({s['on'] for s in b.settlements}):
                due_cash = [s for s in b.settlements if s['on'] <= due]
                b.settled_cash += sum(s['amount'] for s in due_cash)
                b.settlements = [s for s in b.settlements if s['on'] > due]
                f.reconcile(b, due, 10_000_000)
    results = {a: f.metrics(b, curves[a], 10_000_000, stop) for a, b in books.items()}
    return {'scenario': scenario, 'phase': phase, 'results': results, 'trace': trace, 'curves': curves}


def grade(groups):
    failures, missing = [], []
    for s in SCENARIOS:
        gs = [g for g in groups if g['scenario'] == s]
        if len(gs) != 5: missing.append(s+':missing_phases')
        for g in gs:
            r = g['results']['two_head_net']
            if r['complete'] and r['price_mdd_pct'] < -10:
                failures.append(f'{s}/{g["phase"]}:drawdown')
        def complete(a):
            return len(gs) == 5 and all(g['results'][a]['complete'] for g in gs)
        def avg(a):
            return float(np.mean([g['results'][a]['net_return_pct'] for g in gs]))
        def worst(a):
            return min(g['results'][a]['price_mdd_pct'] for g in gs)
        if s in SCENARIOS[:2]:
            for a in ('ridge14', 'single_net_tree', 'momentum12_skip21'):
                if complete(a) and any(g['results']['two_head_net']['complete'] and
                    g['results']['two_head_net']['price_mdd_pct'] < worst(a) for g in gs):
                    failures.append(s+':known_path_worse_than_complete_control:'+a)
        if complete('two_head_net'):
            if avg('two_head_net') <= 0: failures.append(s+':mean_nonpositive')
            if worst('two_head_net') < -10: failures.append(s+':drawdown')
            controls = ['ridge14', 'momentum12_skip21', 'momentum_same_slots']
            if s in SCENARIOS[:2]: controls.append('single_net_tree')
            for a in controls:
                if complete(a):
                    if avg('two_head_net') <= avg(a): failures.append(s+':not_above:'+a)
                    if s in SCENARIOS[:2] and a != 'momentum_same_slots' and worst('two_head_net') < worst(a):
                        failures.append(s+':worse_drawdown:'+a)
        missing.extend(f'{s}/{g["phase"]}/{a}' for g in gs for a, r in g['results'].items() if not r['complete'])
    decision = 'rejected' if failures else 'inconclusive_data' if missing else 'conditional_development_support'
    if not missing and groups and all(g['results']['two_head_net'].get('buy_fills') == 0 for g in groups):
        decision = 'no_economic_edge'
    if decision == 'conditional_development_support':
        gains = defaultdict(float)
        for g in groups:
            if g['scenario'] == '75bp_full':
                for t, v in g['results']['two_head_net']['pnl_by_ticker'].items(): gains[t] += v/5
        if not gains or sum(gains.values())-max(gains.values()) <= 0: decision = 'concentrated_support'
    return {'decision': decision, 'violations': failures, 'unavailable': missing,
            'live_order_eligible': False}


def run(root, commit, output):
    import sklearn
    import scipy
    from threadpoolctl import threadpool_limits
    if sklearn.__version__ != '1.7.2': raise ValueError('research_sklearn_version_mismatch')
    if root != old.ROOT.resolve(): raise ValueError('wrong_repository')
    if output.exists() or output.with_suffix('.models.pkl').exists(): raise FileExistsError(output)
    start = time.monotonic()
    provenance = {}
    for name in (PLAN, PROSE):
        body = (root/name).read_bytes()
        if body != subprocess.check_output(['git', 'show', f'{PLAN_COMMIT}:{name}'], cwd=root):
            raise ValueError('plan_drift')
        provenance[name] = f.sha(body)
    for name in CODE:
        body = (root/name).read_bytes()
        if body != subprocess.check_output(['git', 'show', f'{commit}:{name}'], cwd=root):
            raise ValueError('implementation_not_precommitted')
        provenance[name] = f.sha(body)
        if name.endswith('.py'):
            module_name = name.removeprefix('src/').removesuffix('.py').replace('/', '.')
            module = sys.modules.get(module_name)
            if module is not None and Path(module.__file__).resolve() != root/name:
                raise ValueError('import_outside_declared_repository:'+name)
    if Path(__file__).resolve() != root/CODE[0]: raise ValueError('wrong_runner')
    plan = json.loads((root/PLAN).read_bytes())
    cfg = old.plan()
    scope = json.loads(f.fixed_bytes(old.SCOPE, old.SCOPE_SHA))
    f.fixed_bytes(old.FINANCIAL, old.FINANCIAL_SHA)
    days, sources = old.load_krx_archives([root/'data/research'/r['archive'] for r in scope['sources'][:9]])
    if sources != scope['sources'][:9]: raise ValueError('source_drift')
    panel = old.PricePanel(days)
    by_day = {d: {r['ISU_CD']: r for r in rs} for d, rs in days.items()}
    financials = old.load_financials(old.FINANCIAL)['records']
    roster = {r['ISU_CD'] for r in old.top200(days[cfg['financial_roster_asof']])}
    rows, grouped = [], {}
    for day in panel.sessions:
        if not '2024-04-01' <= day <= '2024-12-11': continue
        if '2024-09-20' < day < '2024-10-02': continue
        rs, *_ = old.features_at(day, panel, by_day, financials, roster)
        grouped[day] = [{**r, **old.label_at(day, r['ticker'], panel, by_day, 5)} for r in rs]
        if day <= '2024-09-20': rows.extend(grouped[day])
    training_sha = old.encoded_digest([r for r in rows if r['state'] == 'matured'])
    if training_sha != plan['source_hashes']['original_training']: raise ValueError('training_drift')
    folds, models, fits = [], [], 0
    with threadpool_limits(limits=1):
        for lo, hi in plan['internal_folds'] + [plan['account_evaluation']]:
            model = fit(rows, cfg, lo, plan)
            fits += model['fit_calls']; models.append(model)
            records, predictions = [], {}
            for day, rs in grouped.items():
                if not lo <= day <= hi: continue
                scores, heads = predict(rs, model); predictions[day] = scores
                record = {'date': day, **{a: temporal.cohort(rs, scores[a]) for a in ARMS},
                          'predictions': scores, 'gain': heads[0].tolist(), 'loss': heads[1].tolist()}
                record['common_missing'] = any(record[a]['missing'] for a in ARMS)
                records.append(record)
            folds.append({'start': lo, 'end': hi, 'training': model['ridge'],
                'training_sha': model['training_sha'], 'records': records,
                'pair_missing_dates': {a: [r['date'] for r in records if
                    r['two_head_net']['missing'] or r[a]['missing']] for a in ARMS[1:]},
                'paths': [temporal.path([{**r, 'common_missing': bool(r[a]['missing'])} for r in records], a, c, p)
                          for a in ARMS for c in (75,120) for p in range(5)] if lo < '2024-10-01' else []})
    # Read-only archived unit facts; does not reopen source collection or use old forecasts.
    frozen_plan = json.loads(f.fixed_bytes(root/f.PLAN_PATH, f.PLAN_SHA))
    archive = frozen_plan['inputs']['forecast_archive']
    saved = json.loads(f.fixed_bytes(root/archive['path'], archive['sha256']))
    reviewed = {(r['listing_session'], r['ticker']): r for r in saved['reviewed_facts']}
    inputs = f.Inputs({}, {}, by_day, {d: panel.sessions[i-1] for i,d in enumerate(panel.sessions) if i}, reviewed, {})
    groups = [replay(inputs, predictions, frozen_plan['execution'], s, p) for s in SCENARIOS for p in range(5)]
    model_path = output.with_suffix('.models.pkl')
    with model_path.open('xb') as stream: pickle.dump(models, stream)
    result = {'candidate': plan['candidate'], 'implementation_commit': commit, 'provenance': provenance,
        'sources': sources, 'training_sha': training_sha, 'folds': folds, 'groups': groups,
        'verdict': grade(groups), 'fit_calls': fits, 'cohort_paths': 160, 'account_paths': 100,
        'complete_accounts': sum(r['complete'] for g in groups for r in g['results'].values()),
        'sklearn_version': sklearn.__version__, 'numpy_version': np.__version__, 'scipy_version': scipy.__version__,
        'peak_rss_native_units': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'model_file': str(model_path.relative_to(root)), 'model_sha': f.sha(model_path.read_bytes()),
        'elapsed_seconds': time.monotonic()-start, 'new_data_requests': 0,
        'pristine_oos': False, 'verified_total_return': False, 'production_changed': False,
        'integrated_roadmap_percent': 40}
    with output.open('x') as stream: json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({k: result[k] for k in ('verdict','complete_accounts','fit_calls','elapsed_seconds')}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--implementation-commit', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not args.output.resolve().is_relative_to(args.repo.resolve()/'data/research'):
        p.error('research output required')
    run(args.repo.resolve(), args.implementation_commit, args.output.resolve())
