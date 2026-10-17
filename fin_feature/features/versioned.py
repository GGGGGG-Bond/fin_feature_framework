"""Immutable calculation logs and current feature projections for version-backed input."""
import json
import math
from pathlib import Path
import subprocess
import uuid

from fin_feature.cleaning.versioned import VersionedCleanRepository, digest
from fin_feature.features.semantic import FIELD_AUTHORITY, FALLBACK_TABLE
from fin_feature.features.registry import FeatureRegistry
from fin_feature.storage.hashing import canonical_json
from fin_feature.utils.date_utils import previous_report_date, year_ago_report_date

# Each dependency describes standardized financial fields, not API-specific columns.
DEPENDENCIES = {
    'net_profit_margin': ('current', ('parent_netprofit', 'total_operate_income')),
    'gross_margin_feat': ('current', ('gross_margin',)),
    'roe_change': ('yoy', ('weightavg_roe',)),
    'roe_change_rate': ('yoy', ('weightavg_roe',)),
    'revenue_yoy': ('yoy', ('total_operate_income',)),
    'profit_yoy': ('yoy', ('parent_netprofit',)),
    'revenue_qoq': ('qoq', ('total_operate_income',)),
    'profit_qoq': ('qoq', ('parent_netprofit',)),
    'debt_asset_ratio_change': ('previous', ('total_assets', 'total_liabilities')),
    'current_ratio_change': ('previous', ('current_ratio',)),
    'equity_ratio': ('current', ('total_assets', 'total_equity')),
    'debt_asset_ratio': ('current', ('total_assets', 'total_liabilities')),
}


def code_identity():
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True))
        return {'commit': commit, 'dirty': dirty}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': None}


def _dates(date, kind):
    result = [date]
    if kind == 'yoy':
        result.append(year_ago_report_date(date))
    elif kind in ('previous', 'qoq'):
        previous = previous_report_date(date)
        result.append(previous)
        # previous Q1 is already a single quarter; other previous periods need their predecessor.
        if kind == 'qoq' and previous and not previous.endswith('03-31'):
            result.append(previous_report_date(previous))
    return [date for date in result if date]


def input_lineage(adapter, company, date, feature_name):
    kind, fields = DEPENDENCIES[feature_name]
    inputs = []
    for period in _dates(date, kind):
        for field in fields:
            for role, table in [('authority', FIELD_AUTHORITY[field]), ('fallback_candidate', FALLBACK_TABLE.get(field))]:
                if table is None:
                    continue
                row = adapter.results.get((company, period, table))
                inputs.append({'report_date': period, 'field': field, 'table_type': table, 'role': role,
                               'record_ref': row['record_ref'] if row else None,
                               'clean_ref': row['clean_id'] if row else None,
                               'status': row['status'] if row else 'missing',
                               'evidence_sufficient': row['version']['evidence_sufficient'] if row else 0})
    if feature_name in ('gross_margin_feat', 'current_ratio_change'):
        for table in ('income_statement', 'financial_indicator', 'balance_sheet'):
            row = adapter.results.get((company, date, table))
            inputs.append({'report_date': date, 'table_type': table, 'role': 'industry_context',
                           'record_ref': row['record_ref'] if row else None,
                           'clean_ref': row['clean_id'] if row else None,
                           'status': row['status'] if row else 'missing',
                           'evidence_sufficient': row['version']['evidence_sufficient'] if row else 0})
    return inputs


def _reason(feature, value, ctx, diagnostics, inputs):
    if value is not None:
        return None
    _, fields = DEPENDENCIES[feature.name]
    dates = {i['report_date'] for i in inputs}
    relevant = [d for d in diagnostics if d.get('report_date', ctx.report_date) in dates and
                (d.get('feature') == feature.name or (not d.get('feature') and d.get('field') in fields))]
    priorities = ['calculation_error', 'not_applicable', 'unknown_unit', 'incomparable_basis',
                  'invalid_value', 'non_positive_base', 'non_positive_comparison_base', 'missing_drop']
    reasons = {d.get('reason_code') for d in relevant}
    for reason in priorities:
        if reason in reasons:
            return reason
    if feature.name in ('debt_asset_ratio', 'debt_asset_ratio_change'):
        if ctx.current.get('total_assets') is not None and ctx.current['total_assets'] <= 0:
            return 'non_positive_base'
        if ctx.current.get('total_liabilities') is not None and ctx.current['total_liabilities'] < 0:
            return 'invalid_value'
    if any(i['report_date'] != ctx.report_date and i['role'] == 'authority' and i['status'] != 'kept' for i in inputs):
        return 'missing_comparison_period'
    return 'missing_input'


def evaluate_feature(adapter, company, date, feature_name):
    """One pure business calculation shared by normal runs and offline replay."""
    from fin_feature.features.engine import _build_context
    from fin_feature.features.base import FeatureContext
    cls = FeatureRegistry.get_by_name(feature_name)
    if cls is None or feature_name not in DEPENDENCIES:
        raise ValueError('unsupported feature dependency declaration')
    ctx = _build_context(adapter, company, date)
    if ctx is None:
        ctx = FeatureContext(company, date, {}, None, None, diagnostics=[])
    context_diags = list(ctx.diagnostics or [])
    ctx.diagnostics = []
    feature = cls()
    try:
        value = feature.calculate(ctx)
        if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
            value = None
            ctx.diagnostics.append({'feature': feature.name, 'reason_code': 'invalid_value'})
    except Exception:
        value = None
        ctx.diagnostics.append({'feature': feature.name, 'reason_code': 'calculation_error'})
    inputs = input_lineage(adapter, company, date, feature.name)
    diagnostics = context_diags + ctx.diagnostics
    for clean_id in sorted({i['clean_ref'] for i in inputs if i['clean_ref']}):
        row = adapter.results_by_id[clean_id]
        diagnostics += [{**d, 'report_date': row['version']['report_date']} for d in json.loads(row['diagnostics'])]
    kind, fields = DEPENDENCIES[feature.name]
    allowed_fields = set(fields) | ({'current_sq', 'previous_sq'} if kind == 'qoq' else set())
    dates = {i['report_date'] for i in inputs}
    diagnostics = [d for d in diagnostics if d.get('report_date', date) in dates and
                   (d.get('feature') == feature.name or (not d.get('feature') and d.get('field') in allowed_fields))]
    return {'feature_value': value, 'not_computable_reason': _reason(feature, value, ctx, diagnostics, inputs),
            'inputs': inputs, 'diagnostics': diagnostics}


def compute_versioned(repo, groups=None, config=None):
    from fin_feature.features.engine import DEFINITION_VERSION, _build_context
    from fin_feature.features.base import FeatureContext
    classes = FeatureRegistry.get_all()
    if groups:
        unknown = set(groups) - {cls.group for cls in classes}
        if unknown:
            raise ValueError('unknown feature groups: ' + ', '.join(sorted(unknown)))
        classes = [cls for cls in classes if cls.group in groups]
    if any(cls.name not in DEPENDENCIES for cls in classes):
        raise ValueError('version-backed features require declared lineage dependencies')
    identity = code_identity()
    compute_run_id = str(uuid.uuid4())
    written = 0
    # Same transaction covers version selection, calculations, logs and current projection.
    with repo.unit_of_work():
        adapter = VersionedCleanRepository(repo, config)
        pairs = sorted({(company, date) for company, date, _ in adapter.results})
        for company, date in pairs:
            for cls in classes:
                feature = cls()
                calculated = evaluate_feature(adapter, company, date, feature.name)
                value, reason = calculated['feature_value'], calculated['not_computable_reason']
                inputs, diagnostics = calculated['inputs'], calculated['diagnostics']
                version_ids = canonical_json(sorted({i['record_ref'] for i in inputs if i['record_ref']}))
                snapshot = canonical_json({'pipeline_version': 'stage2c-v1', 'cleaning': adapter.config,
                                           'inputs': inputs, 'diagnostics': diagnostics,
                                           'code_identity': identity})
                repo.save_compute_log(compute_run_id, company, date, feature.name, DEFINITION_VERSION,
                                      version_ids, value, reason, identity['commit'], snapshot, None)
                record = {'secucode': company, 'report_date': date, 'feature_name': feature.name,
                          'feature_value': value, 'feature_group': feature.group,
                          'definition_version': DEFINITION_VERSION, 'compute_run_id': compute_run_id,
                          'input_version_ids': version_ids, 'code_commit': identity['commit'],
                          'config_hash': digest(adapter.config), 'as_of_date': None,
                          'not_computable_reason': reason}
                written += repo.upsert_versioned_feature(record)
    return written
