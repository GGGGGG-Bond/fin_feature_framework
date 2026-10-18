"""Rolling formation and cost sensitivity using declared public signal editions.

This remains a fractional-position price-return study. Fixed signals may be
reused at later formations; no unavailable quarter or tradability is invented.
"""
import csv
import json
from pathlib import Path

from fin_feature.addons.returns import _availability, _date, _metrics, run_return_backtest
from fin_feature.storage.hashing import canonical_json


def select_public_signals(signals, formation_date, universe):
    if (not isinstance(signals, dict) or signals.get('schema') != 'return-signals-v1'
            or not isinstance(signals.get('records'), list)
            or not all(isinstance(r, dict) for r in signals['records'])
            or not isinstance(signals.get('feature_name'), str) or not signals['feature_name']):
        raise ValueError('return-signals-v1 with feature_name and record objects required')
    formation = _date(formation_date)
    if len(universe) != len(set(universe)) or not universe:
        raise ValueError('nonempty unique study universe required')
    selected, missing = [], []
    for code in sorted(universe):
        candidates = []
        for row in signals['records']:
            if row['secucode'] != code:
                continue
            public = _availability(row.get('available_at'))
            if public is not None and public <= formation and _date(row['report_date']) <= formation:
                candidates.append(((row['report_date'], public), row))
        if not candidates:
            missing.append(dict(secucode=code, reason='no_public_signal_at_formation'))
            continue
        best = max(key for key, _ in candidates)
        editions = {canonical_json(row) for key, row in candidates if key == best}
        if len(editions) != 1:
            raise ValueError('ambiguous equally dated public editions: ' + code)
        selected.append(json.loads(next(iter(editions))))
    return dict(schema='return-signals-v1', feature_name=signals['feature_name'], records=selected), missing


def run_return_study(signals, prices, *, windows, universes, costs=(0, 10, 30), benchmark='510300.SH'):
    """Reform groups each window; chained NAV is flat between exit and next open.

    Use adjacent non-overlapping requested boundaries. Every window pays entry
    and exit costs, including the benchmark. Missing members/prices make that
    scenario incomplete, with no silent universe deletion or partial chaining.
    """
    if not windows or not universes or not costs or len(set(costs)) != len(costs):
        raise ValueError('windows, universes and distinct costs required')
    previous_end = None
    for window in windows:
        start, end = _date(window['formation_date']), _date(window['end_date'])
        if end <= start or (previous_end is not None and start != previous_end):
            raise ValueError('rolling windows must be ordered and share adjacent boundaries')
        previous_end = end
    scenarios = []
    for universe in universes:
        for cost in costs:
            runs, complete = [], True
            for window in windows:
                selected, excluded = select_public_signals(signals, window['formation_date'], universe['companies'])
                result = run_return_backtest(selected, prices, **window, n_groups=universe['groups'], cost_bps=cost, benchmark=benchmark)
                result['formation_missing_signals'] = excluded
                # Keep the planned denominator even if enough remaining names
                # could technically fill the requested number of groups.
                if excluded or result['excluded_signals']:
                    result.update(status='incomplete_signals', exit_code=1)
                complete = complete and result['exit_code'] == 0
                runs.append(result)
            scenario = dict(universe=universe['name'], companies=universe['companies'], groups=universe['groups'],
                            cost_bps_per_side=cost, status='success' if complete else 'incomplete',
                            windows=runs, chained_nav=[], chained_metrics={})
            if complete:
                for series in ['benchmark', *[f'G{i}' for i in range(1, universe['groups'] + 1)]]:
                    nav, wealth = [], 1.0
                    for run in runs:
                        points = [p for p in run['nav'] if p['series'] == series]
                        # Over the interval from previous exit to next entry,
                        # cash has zero return; skip the duplicated initial NAV.
                        for point in points:
                            if nav and point['date'] == nav[-1]['date']:
                                continue
                            nav.append(dict(date=point['date'], nav=wealth * point['nav']))
                        wealth *= points[-1]['nav']
                    scenario['chained_nav'].extend(dict(series=series, **p) for p in nav)
                    scenario['chained_metrics'][series] = _metrics(nav, runs[0]['entry_date'], runs[-1]['exit_date'])
            scenarios.append(scenario)
    return dict(schema='return-study-v1', status='success' if all(s['status'] == 'success' for s in scenarios) else 'incomplete',
                exit_code=0 if all(s['status'] == 'success' for s in scenarios) else 1,
                scenarios=scenarios, windows=windows, cost_grid_bps=list(costs),
                universe_selection='Explicit manually selected subsets; no representative market or industry claim.',
                execution='Fractional equal initial weights; sell at each window end close; reform and buy next session open; cash overnight earns zero.',
                limitations=['Signals are selected by declared availability at each formation, not historical system observations.',
                             'The default study reuses fixed 2024 annual signals; it does not imply new quarterly signal updates.',
                             'Adjusted prices, small hand-picked universes; no lot, liquidity, suspension or limit-price execution model.',
                             'Costs are symmetric per-side scenarios, not a complete tax or broker schedule.'])


def export_return_study(result, output):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    (root / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    rows = []
    for scenario in result['scenarios']:
        for series, metrics in scenario['chained_metrics'].items():
            rows.append(dict(universe=scenario['universe'], cost_bps_per_side=scenario['cost_bps_per_side'],
                             series=series, planned_companies=len(scenario['companies']), windows=len(scenario['windows']), **metrics))
    fields = ['universe', 'cost_bps_per_side', 'series', 'planned_companies', 'windows', 'net_return', 'max_drawdown', 'annualized_return', 'annualized_volatility']
    with (root / 'sensitivity.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    return rows


def cmd_return_study(args):
    import sys
    try:
        signals, prices, plan = [json.loads(Path(p).read_bytes()) for p in (args.signals, args.prices, args.plan)]
        if plan.get('schema') != 'return-study-plan-v1':
            raise ValueError('return-study-plan-v1 required')
        result = run_return_study(signals, prices, windows=plan['windows'], universes=plan['universes'],
                                  costs=plan['cost_grid_bps'], benchmark=plan.get('benchmark', '510300.SH'))
        export_return_study(result, args.output_dir)
        print(json.dumps(dict(status=result['status'], scenarios=len(result['scenarios'])), ensure_ascii=False))
        return result['exit_code']
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        print(f'Return study failed: {exc}', file=sys.stderr)
        return 2
