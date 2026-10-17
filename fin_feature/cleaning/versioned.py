"""Version-backed cleaning; standardized values never pass through conversion again."""
import copy
import hashlib
import json
import math

from fin_feature.cleaning.pipeline import CLEAN_FIELDS, CleanStats
from fin_feature.cleaning import rules
from fin_feature.storage.hashing import canonical_json, compute_metric_hash, compute_semantic_hash
from fin_feature.utils.date_utils import get_disclosure_status, get_report_type, normalize_date
from fin_feature.storage.clean_model import CLEAN_RESULT_COLUMNS

TABLE_TYPES = ('financial_indicator', 'income_statement', 'balance_sheet')
IDENTITY_FIELDS = {'secucode', 'security_code', 'security_name', 'report_date', 'notice_date',
                   'qdate', 'datatype', 'report_type', 'industry_name', 'disclosure_status'}
PIPELINE_VERSION = 'stage2c-v1'


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def effective_rules(config=None):
    """Pure configuration resolution, independent of legacy runtime globals."""
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError('cleaning must be an object')
    for key in ('outlier_thresholds', 'missing_strategy'):
        if key in config and not isinstance(config[key], dict):
            raise ValueError(f'cleaning.{key} must be an object')
    rules._validate_config(config)
    result = {'pipeline_version': PIPELINE_VERSION,
              'outlier_thresholds': copy.deepcopy(rules._DEFAULT_OUTLIER_THRESHOLDS),
              'missing_strategy': copy.deepcopy(rules._DEFAULT_MISSING_STRATEGY),
              'default_missing_strategy': rules._DEFAULT_DEFAULT_MISSING_STRATEGY,
              'amount_must_positive': sorted(rules.AMOUNT_MUST_POSITIVE)}
    for key, bounds in config.get('outlier_thresholds', {}).items():
        if not isinstance(key, str):
            raise ValueError('cleaning field names must be strings')
        result['outlier_thresholds'][key] = [float(bounds[0]), float(bounds[1])]
    for key, strategy in config.get('missing_strategy', {}).items():
        if not isinstance(key, str):
            raise ValueError('cleaning field names must be strings')
        if key == 'default':
            result['default_missing_strategy'] = strategy
        else:
            result['missing_strategy'][key] = strategy
    # Canonical JSON also removes tuple/list differences between defaults and YAML.
    return json.loads(canonical_json(result))


def current_versions(repo):
    rows = [dict(row) for row in repo.conn.execute(
        "SELECT * FROM statement_version WHERE source_name='eastmoney' AND is_superseded=0 "
        'ORDER BY secucode, report_date, table_type')]
    seen = set()
    for row in rows:
        date = row['report_date']
        if normalize_date(date) != date or date[5:] not in ('03-31','06-30','09-30','12-31'):
            raise ValueError('statement report_date must be a normalized quarter end')
        key = (row['secucode'], row['report_date'], row['table_type'])
        if row['table_type'] not in TABLE_TYPES or key in seen:
            raise ValueError('non-unique or unsupported current statement group')
        seen.add(key)
        values = json.loads(row['standardized_fields'])
        if not isinstance(values, dict) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in values.values() if v is not None
        ):
            raise ValueError('statement standardized_fields must contain finite numbers')
        semantic = {k: row[k] for k in ('source_name', 'currency', 'consolidate_scope',
                                       'period_basis', 'amount_unit', 'industry_type')}
        if compute_metric_hash(values) != row['metric_hash'] or compute_semantic_hash(values, **semantic) != row['semantic_hash']:
            raise ValueError('statement content hash mismatch')
        observation = repo.conn.execute(
            'SELECT normalization_diagnostics FROM statement_observation WHERE version_id=? ORDER BY rowid LIMIT 1',
            (row['version_id'],)).fetchone()
        row['normalization_diagnostics'] = observation[0] if observation is not None else None
    return rows


def _prepare(version, config):
    table = version['table_type']
    raw = json.loads(version['raw_fields'] or '{}')
    original = json.loads(version['standardized_fields'])
    values = {name: original.get(name) for name in CLEAN_FIELDS[table] if name not in IDENTITY_FIELDS}
    compatibility = {name: raw.get(name) for name in CLEAN_FIELDS[table]}
    compatibility.update(secucode=version['secucode'], report_date=version['report_date'],
                         notice_date=version['notice_date'])
    diagnostics = json.loads(version.get('normalization_diagnostics') or '[]')
    blocked = {d.get('field') for d in diagnostics if d.get('reason_code') in ('unknown_unit', 'invalid_value')}
    from fin_feature.features.standardize import FIELD_UNITS
    for name in values:
        if values[name] is None and (raw.get(name) is not None or FIELD_UNITS.get(name) == 'unknown') and name not in blocked:
            reason = 'unknown_unit' if FIELD_UNITS.get(name) == 'unknown' else 'invalid_value'
            blocked.add(name)
            diagnostics.append({'field': name, 'reason_code': reason})
    dropped = False
    for name, value in values.items():
        if value is None:
            strategy = config['missing_strategy'].get(name, config['default_missing_strategy'])
            if strategy == 'drop':
                dropped = True
                diagnostics.append({'field': name, 'reason_code': 'missing_drop'})
            elif strategy == 'zero' and name not in blocked and raw.get(name) is None:
                values[name] = compatibility[name] = 0
                diagnostics.append({'field': name, 'reason_code': 'missing_zero'})
            # No zero fill can manufacture a verified unit or repair invalid input.
            continue
        bounds = config['outlier_thresholds'].get(name)
        if bounds is not None and not bounds[0] <= value <= bounds[1]:
            diagnostics.append({'field': name, 'reason_code': 'outlier_threshold', 'standard_value': value})
        if name in config['amount_must_positive'] and value < 0:
            diagnostics.append({'field': name, 'reason_code': 'outlier_negative'})
    compatibility['report_type'] = get_report_type(version['report_date'])
    compatibility['disclosure_status'] = get_disclosure_status(version['report_date'], version['notice_date'])
    if compatibility['disclosure_status'] in ('delayed', 'not_disclosed'):
        diagnostics.append({'field': 'notice_date', 'reason_code': 'time_align_' + compatibility['disclosure_status']})
    return values, compatibility, diagnostics, 'dropped' if dropped else 'kept'


def run_versioned_cleaning(repo, config=None):
    effective = effective_rules(config)
    config_json, config_hash = canonical_json(effective), digest(effective)
    stats = {table: CleanStats(table) for table in TABLE_TYPES}
    with repo.unit_of_work():
        versions = current_versions(repo)
        old = {tuple(row[:3]): row[3] for row in repo.conn.execute('SELECT secucode, report_date, table_type, clean_ref FROM clean_selection')}
        for table in TABLE_TYPES:
            for r in repo.conn.execute(f'SELECT * FROM clean_{table}'):
                old.setdefault((r['secucode'], r['report_date'], table), r['clean_ref'])
        prepared = []
        new = {}
        for version in versions:
            table = version['table_type']
            stats[table].raw_count += 1
            stats[table].dedup_count += 1
            values, compat, diagnostics, status = _prepare(version, effective)
            clean_id = digest({'record_ref': version['version_id'], 'config_hash': config_hash})
            fields = (clean_id, version['version_id'], config_hash, config_json, status,
                      canonical_json(values), canonical_json(compat), canonical_json(diagnostics))
            existing = repo.conn.execute('SELECT * FROM clean_statement WHERE clean_id=?', (clean_id,)).fetchone()
            if existing is None:
                repo.conn.execute('INSERT INTO clean_statement (' + ','.join(CLEAN_RESULT_COLUMNS) + ') VALUES (?,?,?,?,?,?,?,?)', fields)
                for d in diagnostics:
                    repo.log_clean_action(version['secucode'], version['report_date'], table,
                                          d.get('reason_code', 'unknown'), canonical_json(d))
            elif tuple(existing[c] for c in CLEAN_RESULT_COLUMNS) != fields:
                raise ValueError('immutable cleaning result conflicts with stored content')
            key = (version['secucode'], version['report_date'], table)
            new[key] = clean_id
            if status == 'dropped':
                stats[table].dropped_count += 1
                continue
            stats[table].clean_count += 1
            compat.update(record_ref=version['version_id'], clean_ref=clean_id)
            prepared.append((table, compat))
        changed = {key[0] for key in set(old) | set(new) if old.get(key) != new.get(key)}
        # The compatibility tables are current projections. Immutable history lives above.
        for table in TABLE_TYPES:
            repo.conn.execute(f'DELETE FROM clean_{table}')
        repo.conn.execute('DELETE FROM clean_selection')
        repo.conn.executemany('INSERT INTO clean_selection VALUES (?,?,?,?)',
                              [(*key, clean_id) for key, clean_id in new.items()])
        for table, record in prepared:
            repo.upsert_clean(table, record)
        for company in changed:
            repo.conn.execute("DELETE FROM features WHERE secucode=? AND definition_version='stage1-v1'", (company,))
    return list(stats.values())


class VersionedCleanRepository:
    """Read-only adapter for the existing semantic merge/context engine."""
    def __init__(self, repo, config=None):
        self.repo = repo
        self.config = effective_rules(config)
        self.config_hash = digest(self.config)
        self.records, self.results = {}, {}
        selections = {tuple(row[:3]): row[3] for row in repo.conn.execute('SELECT secucode, report_date, table_type, clean_ref FROM clean_selection')}
        for version in current_versions(repo):
            key = (version['secucode'], version['report_date'], version['table_type'])
            clean = repo.conn.execute('SELECT * FROM clean_statement WHERE record_ref=? AND config_hash=?',
                                      (version['version_id'], self.config_hash)).fetchone()
            if clean is None:
                raise ValueError('stale_clean: run clean for the current statement and configuration')
            clean = dict(clean)
            if selections.get(key) != clean['clean_id']:
                raise ValueError('stale_clean: selection identity mismatch')
            # Reconstruct from immutable version+rules to reject tampered sidecars.
            vals, compat, diagnostics, status = _prepare(version, self.config)
            if (clean['status'] != status or clean['standardized_fields'] != canonical_json(vals)
                or clean['config_snapshot'] != canonical_json(self.config)
                or clean['compatibility_fields'] != canonical_json(compat)
                or clean['diagnostics'] != canonical_json(diagnostics)
                or clean['clean_id'] != digest({'record_ref': version['version_id'], 'config_hash': self.config_hash})):
                raise ValueError('cleaning result does not match immutable version and rules')
            projection = repo.get_clean_record(version['table_type'], version['secucode'], version['report_date'])
            if status == 'kept' and (projection is None or projection['record_ref'] != version['version_id']
                                     or projection['clean_ref'] != clean['clean_id']):
                raise ValueError('stale_clean: current clean projection identity mismatch')
            if status == 'dropped' and projection is not None:
                raise ValueError('stale_clean: dropped statement still has current projection')
            self.results[key] = {**clean, 'version': version}
            if status == 'kept':
                record = {**compat, **vals, 'record_ref': version['version_id'], 'clean_ref': clean['clean_id'],
                          'industry_type': version['industry_type'], '__standardized__': True,
                          '__diagnostics__': diagnostics}
                self.records[key] = record
        if set(selections) != set(self.results):
            raise ValueError('stale_clean: selection groups differ from current statements')
        self.results_by_id = {row['clean_id']: row for row in self.results.values()}
        # Never accept unrelated, missing or stale version-backed projection rows.
        for table in TABLE_TYPES:
            for row in repo.conn.execute(f'SELECT secucode, report_date FROM clean_{table}'):
                if (row[0], row[1], table) not in self.records:
                    raise ValueError('stale_clean: extra current projection; run clean')

    def get_clean_record(self, table, company, date):
        return self.records.get((company, date, table))

    def get_all_clean_secucode_dates(self, table):
        return sorted((company, date) for company, date, kind in self.results if kind == table)
