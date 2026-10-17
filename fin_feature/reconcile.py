"""Offline explicit-semantics comparison; never overwrites primary data."""
import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

FIELDS = {
    'total_operate_income': ('IS', 'flow_ytd'),
    'parent_netprofit': ('IS', 'flow_ytd'),
    'operate_cost': ('IS', 'flow_ytd'),
    'total_assets': ('BS', 'stock_end'),
    'total_liabilities': ('BS', 'stock_end'),
    'total_equity': ('BS', 'stock_end'),
}
UNITS = {'yuan': Decimal(1), 'wan_yuan': Decimal(10000), 'yi_yuan': Decimal(100000000)}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) <= Decimal('1e30') else None
    except (InvalidOperation, ValueError):
        return None


def index_document(doc):
    if not isinstance(doc, dict) or doc.get('schema') != 'reconcile-v1':
        raise ValueError('expected reconcile-v1 document')
    for key in ('source', 'upstream'):
        if not isinstance(doc.get(key), str) or not doc[key].strip() or doc[key].strip().lower() in ('unknown', 'null'):
            raise ValueError(f'explicit {key} required')
    if not isinstance(doc.get('records'), list) or not doc['records']:
        raise ValueError('non-empty records list required')
    result = {}
    for row in doc['records']:
        if not isinstance(row, dict):
            raise ValueError('record must be an object')
        security, period, field = (row.get(k) for k in ('secucode', 'report_date', 'field'))
        if not isinstance(security, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', security):
            raise ValueError('canonical secucode required')
        if not isinstance(period, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', period):
            raise ValueError('canonical report_date required')
        date.fromisoformat(period)
        if period[5:] not in ('03-31', '06-30', '09-30', '12-31') or not isinstance(field, str) or field not in FIELDS:
            raise ValueError('unsupported period or field')
        key = (security, period, field)
        if key in result:
            raise ValueError(f'ambiguous duplicate record: {key}')
        result[key] = row
    return result


def known(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in ('unknown', 'null')


def compare_documents(left, right, *, abs_tol='1', rel_tol='0.000001'):
    """Amounts use Decimal; tolerance is in normalized currency units, never FX."""
    absolute, relative = number(abs_tol), number(rel_tol)
    if absolute is None or relative is None or absolute < 0 or not 0 <= relative <= 1:
        raise ValueError('invalid nonnegative tolerances (relative <= 1)')
    li, ri = index_document(left), index_document(right)
    if left['upstream'].strip().lower() == right['upstream'].strip().lower():
        raise ValueError('same upstream is not independent source verification')
    rows = []
    for key in sorted(li.keys() | ri.keys()):
        a, b = li.get(key), ri.get(key)
        item = dict(zip(('secucode', 'report_date', 'field'), key))
        item.update(left=a, right=b, status='incomparable', reasons=[])
        if a is None or b is None:
            item.update(status='missing', reasons=['left_record_missing' if a is None else 'right_record_missing'])
        else:
            table, basis = FIELDS[key[2]]
            for name in ('currency', 'scope', 'comparison_basis_id'):
                if not known(a.get(name)) or not known(b.get(name)) or a[name] != b[name]:
                    item['reasons'].append(name + '_unknown_or_mismatch')
            if a.get('currency') != 'CNY':
                item['reasons'].append('unsupported_currency')
            if a.get('scope') not in ('consolidated', 'parent_only'):
                item['reasons'].append('unsupported_scope')
            for side, row in (('left', a), ('right', b)):
                if row.get('table_type') != table or row.get('period_basis') != basis:
                    item['reasons'].append(side + '_table_or_period_basis')
                for name in ('evidence_ref', 'alignment_evidence', 'record_ref'):
                    if not known(row.get(name)):
                        item['reasons'].append(side + '_' + name + '_missing')
                if not isinstance(row.get('unit'), str) or row['unit'] not in UNITS:
                    item['reasons'].append(side + '_unknown_unit')
            av, bv = number(a.get('value')), number(b.get('value'))
            if av is None or bv is None:
                item.update(status='missing', reasons=item['reasons'] + ['missing_or_invalid_value'])
            elif not item['reasons']:
                with localcontext() as ctx:
                    ctx.prec = 60
                    av, bv = av * UNITS[a['unit']], bv * UNITS[b['unit']]
                    delta = abs(av - bv)
                    limit = max(absolute, relative * max(abs(av), abs(bv)))
                    item.update(status='match' if delta <= limit else 'difference',
                                left_normalized=str(av), right_normalized=str(bv),
                                absolute_difference=str(delta), tolerance=str(limit))
        rows.append(item)
    counts = dict(Counter(row['status'] for row in rows))
    comparable = counts.get('match', 0) + counts.get('difference', 0)
    return {'schema': 'reconciliation-report-v1', 'sources': [left['source'], right['source']],
            'upstreams': [left['upstream'], right['upstream']],
            'scope': 'supplied-records-only', 'evidence_verified': False,
            'tolerances': {'absolute': str(absolute), 'relative': str(relative)},
            'total': len(rows), 'comparable': comparable, 'counts': counts,
            'comparable_fraction': comparable / len(rows), 'results': rows}


def compare_files(left_path, right_path, **kwargs):
    payloads = [Path(p).read_bytes() for p in (left_path, right_path)]
    documents = [json.loads(p, parse_float=str) for p in payloads]
    result = compare_documents(*documents, **kwargs)
    result['input_sha256'] = [hashlib.sha256(p).hexdigest() for p in payloads]
    return result


def cmd_reconcile(args):
    import sys
    try:
        report = compare_files(args.left, args.right, abs_tol=args.abs_tol, rel_tol=args.rel_tol)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0 if report['counts'].get('match', 0) == report['total'] else 1
    except (ValueError, TypeError, OSError) as exc:
        print(f'Reconciliation failed: {exc}', file=sys.stderr)
        return 2
