"""Small, explicit price-return grouping backtest, independent of growth proxies.

One formation, equal initial capital, next benchmark session open -> end close.
Missing prices do not silently remove constituents or change formation groups.
"""
import csv
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics


def _date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('normalized ISO date required')
    return date.fromisoformat(value)


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a price or feature')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('finite number required')
    return result


def _availability(value):
    if value is None:
        return None
    if isinstance(value, str) and len(value) == 10:
        return _date(value)
    if not isinstance(value, str):
        raise ValueError('availability must be an ISO date or zoned timestamp')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.utcoffset() is None:
        raise ValueError('availability timestamps need a timezone')
    # Date-only formations are end-of-day in China, the exchange of this scope.
    from datetime import timezone, timedelta
    return instant.astimezone(timezone(timedelta(hours=8))).date()


def _prices(document):
    if not isinstance(document, dict) or document.get('schema') != 'captured-adjusted-prices-v1' or not isinstance(document.get('instruments'), list):
        raise ValueError('captured-adjusted-prices-v1 required')
    result = {}
    for instrument in document.get('instruments', []):
        if not isinstance(instrument, dict) or not isinstance(instrument.get('bars'), list):
            raise ValueError('instrument object with bars list required')
        code = instrument.get('secucode')
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}\.(SH|SZ)', code) or code in result:
            raise ValueError('unique canonical SH/SZ price instrument required')
        if instrument.get('price_basis') != 'provider_qfq':
            raise ValueError('explicit provider_qfq prices required; no unadjusted fallback')
        bars = {}
        for bar in instrument.get('bars', []):
            if not isinstance(bar, dict):
                raise ValueError('price bar must be an object')
            day = _date(bar['date']).isoformat()
            if day in bars:
                raise ValueError('duplicate price date')
            prices = {field: _number(bar[field]) for field in ('open', 'close')}
            if any(v <= 0 for v in prices.values()):
                raise ValueError('prices must be positive')
            bars[day] = prices
        result[code] = bars
    return result


def _metrics(nav, entry, exit):
    values = [r['nav'] for r in nav]
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    changes = [b / a - 1 for a, b in zip(values, values[1:])]
    days = max((_date(exit) - _date(entry)).days, 1)
    return dict(net_return=values[-1] - 1, max_drawdown=drawdown,
                annualized_return=values[-1] ** (365.25 / days) - 1,
                annualized_volatility=statistics.stdev(changes) * math.sqrt(252) if len(changes) > 1 else None)


def run_return_backtest(signals, prices, *, formation_date, end_date, n_groups=5,
                        cost_bps=10, benchmark='510300.SH'):
    formation, end = _date(formation_date), _date(end_date)
    if end <= formation or isinstance(n_groups, bool) or not isinstance(n_groups, int) or n_groups < 2:
        raise ValueError('end must follow formation; at least two integer groups required')
    cost_bps = _number(cost_bps)
    if not 0 <= cost_bps < 10000:
        raise ValueError('cost_bps must be in [0,10000) per side')
    if not isinstance(signals, dict) or signals.get('schema') != 'return-signals-v1' or not isinstance(signals.get('records'), list):
        raise ValueError('return-signals-v1 required')
    if not isinstance(signals.get('feature_name'), str) or not signals['feature_name']:
        raise ValueError('one explicit feature_name required')
    indexed = _prices(prices)
    if benchmark not in indexed:
        raise ValueError('benchmark calendar/prices missing')
    calendar = sorted(day for day in indexed[benchmark] if formation_date < day <= end_date)
    if len(calendar) < 2:
        raise ValueError('at least two benchmark trading sessions required')
    # Do not silently end early when the supplied series is truncated.
    if end_date > max(indexed[benchmark]):
        raise ValueError('benchmark does not cover requested end date')
    entry, exit = calendar[0], calendar[-1]
    eligible, excluded, seen = [], [], set()
    for original in signals['records']:
        row = dict(original)
        code = row.get('secucode')
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}\.(SH|SZ)', code) or code in seen:
            raise ValueError('exactly one signal per canonical company required')
        seen.add(code)
        reason = None
        try:
            available = _availability(row.get('available_at'))
            value = _number(row.get('feature_value'))
        except (ValueError, TypeError, OverflowError):
            available, value, reason = None, None, 'invalid_signal'
        if not reason and _date(row['report_date']) > formation:
            reason = 'future_report_period'
        if not reason and available is None:
            reason = 'unknown_availability'
        if not reason and available > formation:
            reason = 'not_yet_available'
        if not reason and not row.get('evidence_ref'):
            reason = 'missing_signal_evidence'
        if reason:
            excluded.append({**row, 'reason': reason})
        else:
            eligible.append({**row, 'feature_value': value})
    eligible.sort(key=lambda r: (r['feature_value'], r['secucode']))
    result = dict(schema='price-return-backtest-v1', feature_name=signals['feature_name'],
        formation_date=formation_date, requested_end=end_date, entry_date=entry, exit_date=exit,
        entry_price='next benchmark session adjusted open', exit_price='end adjusted close',
        price_basis='provider_qfq', grouping='ascending; equal initial weight; fixed groups; no rebalance',
        cost_bps_per_side=cost_bps, benchmark=benchmark, planned_companies=len(seen),
        eligible_companies=len(eligible), excluded_signals=excluded, groups=[], members=[], nav=[],
        availability_check='supplied public-availability declarations, not historical system knowledge',
        historical_archive_verified=False, high_minus_low_net_return=None,
        limitations=['Small manually selected cross-industry universe; not an unbiased market strategy test.',
                    'Fractional positions; no lot, liquidity, limit-up/down or tax-specific execution model.',
                    'Provider-adjusted price returns; no separate cash-dividend reinvestment ledger.',
                    'Historical byte-identical availability of issuer documents is not independently archived.'])
    if len(eligible) < n_groups:
        result.update(status='insufficient_samples', exit_code=1)
        return result
    fee = cost_bps / 10000

    def curve(codes):
        series = [{'date': formation_date, 'nav': 1.0}]
        for day in calendar:
            value = statistics.mean(indexed[c][day]['close'] / indexed[c][entry]['open'] for c in codes) / (1 + fee)
            if day == exit:
                value *= 1 - fee
            series.append({'date': day, 'nav': value})
        return series

    benchmark_nav = curve([benchmark])
    benchmark_metrics = _metrics(benchmark_nav, entry, exit)
    result['benchmark_metrics'] = {**benchmark_metrics,
        'gross_return': indexed[benchmark][exit]['close'] / indexed[benchmark][entry]['open'] - 1}
    result['nav'].extend({**r, 'series': 'benchmark'} for r in benchmark_nav)
    offset = 0
    for group in range(1, n_groups + 1):
        size = len(eligible) // n_groups + (group <= len(eligible) % n_groups)
        rows = eligible[offset:offset + size]
        offset += size
        missing = []
        for row in rows:
            code = row['secucode']
            gaps = [day for day in calendar if day not in indexed.get(code, {})]
            member = {**row, 'group': group, 'initial_weight': 1 / size,
                      'entry_date': entry, 'exit_date': exit, 'missing_price_dates': gaps,
                      'gross_return': None, 'net_return': None, 'status': 'missing_prices' if gaps else 'ok'}
            if gaps:
                missing.append(code)
            else:
                gross = indexed[code][exit]['close'] / indexed[code][entry]['open']
                member.update(entry_open=indexed[code][entry]['open'], exit_close=indexed[code][exit]['close'],
                              gross_return=gross - 1, net_return=gross * (1 - fee) / (1 + fee) - 1)
            result['members'].append(member)
        stat = dict(group=group, size=size, members=[r['secucode'] for r in rows],
                    feature_min=rows[0]['feature_value'], feature_max=rows[-1]['feature_value'],
                    missing_price_members=missing, status='incomplete' if missing else 'ok',
                    gross_return=None, net_return=None, excess_net_return=None,
                    max_drawdown=None, annualized_return=None, annualized_volatility=None)
        if not missing:
            nav = curve(stat['members'])
            stat.update(_metrics(nav, entry, exit))
            stat['gross_return'] = statistics.mean(indexed[c][exit]['close'] / indexed[c][entry]['open'] - 1 for c in stat['members'])
            stat['excess_net_return'] = stat['net_return'] - benchmark_metrics['net_return']
            result['nav'].extend({**r, 'series': f'G{group}'} for r in nav)
        result['groups'].append(stat)
    complete = all(g['status'] == 'ok' for g in result['groups'])
    if complete:
        result['high_minus_low_net_return'] = result['groups'][-1]['net_return'] - result['groups'][0]['net_return']
    result.update(status='success' if complete else 'incomplete_prices', exit_code=0 if complete else 1)
    return result


def export_return_backtest(result, output):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    (root / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    for name in ('groups', 'members', 'nav'):
        rows = result[name]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with (root / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()} for row in rows)
    lines = ['# 价格收益分组回测', '', f"形成日 {result['formation_date']}；入场 {result['entry_date']}；出场 {result['exit_date']}。",
             f"因子 {result['feature_name']}；合格 {result['eligible_companies']}/{result['planned_companies']}；单边成本 {result['cost_bps_per_side']} bps。", '',
             '| 组 | 数量 | 毛收益 | 净收益 | 超额净收益 | 最大回撤 |', '|---|---:|---:|---:|---:|---:|']
    def pct(v): return '缺失' if v is None else f'{v:.2%}'
    for row in result['groups']:
        lines.append(f"| G{row['group']} | {row['size']} | {pct(row['gross_return'])} | {pct(row['net_return'])} | {pct(row['excess_net_return'])} | {pct(row['max_drawdown'])} |")
    lines += ['', '高低组差是两个组合收益之差，不是已实现可交易的多空策略收益。', '', *result['limitations']]
    (root / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def cmd_return_backtest(args):
    import sys
    try:
        payloads = [Path(p).read_bytes() for p in (args.signals, args.prices)]
        result = run_return_backtest(*[json.loads(p) for p in payloads], formation_date=args.formation_date,
            end_date=args.end_date, n_groups=args.groups, cost_bps=args.cost_bps, benchmark=args.benchmark)
        result['input_sha256'] = [hashlib.sha256(p).hexdigest() for p in payloads]
        export_return_backtest(result, args.output_dir)
        print(json.dumps({k: v for k, v in result.items() if k not in ('nav', 'members')}, ensure_ascii=False))
        return result['exit_code']
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        print(f'Return backtest failed: {exc}', file=sys.stderr)
        return 2
