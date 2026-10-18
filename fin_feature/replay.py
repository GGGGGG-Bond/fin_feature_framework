"""Read-only replay of saved feature inputs using the currently installed code."""
import json
import math

from fin_feature.cleaning.versioned import VersionedCleanRepository, _prepare, digest, PIPELINE_VERSION
from fin_feature.features.versioned import code_identity, evaluate_feature, input_lineage
from fin_feature.storage.hashing import canonical_json, compute_metric_hash, compute_semantic_hash
from fin_feature.features.reviewed import comparison_bases


class FrozenInputs(VersionedCleanRepository):
    """Load exact historical clean_ref/version pairs, never current projections."""
    def __init__(self, repo, result):
        snapshot = json.loads(result['config_snapshot'] or '{}')
        if snapshot.get('pipeline_version') != PIPELINE_VERSION or result['definition_version'] != 'stage1-v1':
            raise ValueError('unsupported saved pipeline/definition; replay requires a 2C calculation log')
        self.config = snapshot['cleaning']
        self.config_hash = digest(self.config)
        self.records, self.results, self.results_by_id = {}, {}, {}
        declared_ids = json.loads(result['input_version_ids'])
        inputs = snapshot['inputs']
        if not isinstance(declared_ids, list) or set(declared_ids) != {i['record_ref'] for i in inputs if i['record_ref']}:
            raise ValueError('saved input version list does not match lineage')
        for item in inputs:
            version_id = item['record_ref']
            if version_id is None:
                continue
            key = (result['secucode'], item['report_date'], item['table_type'])
            if key in self.results:
                if self.results[key]['record_ref'] != version_id or self.results[key]['clean_id'] != item['clean_ref']:
                    raise ValueError('conflicting saved input identities')
                continue
            version = repo.conn.execute('SELECT * FROM statement_version WHERE version_id=?', (version_id,)).fetchone()
            clean = repo.conn.execute('SELECT * FROM clean_statement WHERE clean_id=?', (item['clean_ref'],)).fetchone()
            if version is None or clean is None:
                raise ValueError('saved version/cleaning result is missing')
            version, clean = dict(version), dict(clean)
            if (version['secucode'], version['report_date'], version['table_type']) != key or version['source_name'] != 'eastmoney':
                raise ValueError('saved input identity mismatch')
            values = json.loads(version['standardized_fields'])
            semantic = {name: version[name] for name in ('source_name','currency','consolidate_scope','period_basis','amount_unit','industry_type')}
            if compute_metric_hash(values) != version['metric_hash'] or compute_semantic_hash(values, **semantic) != version['semantic_hash']:
                raise ValueError('saved statement hash mismatch')
            observation = repo.conn.execute('SELECT normalization_diagnostics FROM statement_observation WHERE version_id=? ORDER BY rowid LIMIT 1', (version_id,)).fetchone()
            version['normalization_diagnostics'] = observation[0] if observation else None
            vals, compat, diagnostics, status = _prepare(version, self.config)
            expected = {'record_ref': version_id, 'config_hash': self.config_hash,
                        'config_snapshot': canonical_json(self.config), 'status': status,
                        'standardized_fields': canonical_json(vals), 'compatibility_fields': canonical_json(compat),
                        'diagnostics': canonical_json(diagnostics),
                        'clean_id': digest({'record_ref': version_id, 'config_hash': self.config_hash})}
            if any(clean[name] != value for name,value in expected.items()):
                raise ValueError('saved cleaning result conflicts with immutable input/rules')
            row = {**clean, 'version': version}
            self.results[key] = self.results_by_id[clean['clean_id']] = row
            if status == 'kept':
                self.records[key] = {**compat, **vals, 'record_ref': version_id, 'clean_ref': clean['clean_id'],
                                     'industry_type': version['industry_type'], '__standardized__': True}
                self.records[key]['__comparison_bases__'] = comparison_bases(version, self.config)
        if canonical_json(input_lineage(self, result['secucode'], result['report_date'], result['feature_name'])) != canonical_json(inputs):
            raise ValueError('saved field dependency lineage is incomplete or inconsistent')


def replay_result(repo, result_id):
    """Compare business value and NULL reason; never append logs or overwrite features."""
    owns_transaction = not repo.conn.in_transaction
    if owns_transaction:
        repo.conn.execute('BEGIN')
    try:
        stored = repo.get_compute_log(result_id)
        if stored is None:
            raise ValueError('calculation result does not exist')
        result = dict(stored)
        adapter = FrozenInputs(repo, result)
        calculated = evaluate_feature(adapter, result['secucode'], result['report_date'], result['feature_name'])
        before, after = result['feature_value'], calculated['feature_value']
        value_matches = before is None and after is None
        if before is not None and after is not None:
            value_matches = math.isfinite(before) and math.isclose(before, after, rel_tol=1e-12, abs_tol=1e-12)
        reason_matches = result['not_computable_reason'] == calculated['not_computable_reason']
        current = code_identity()
        original = json.loads(result['config_snapshot']).get('code_identity', {})
        return {'result_id': result_id, 'feature_name': result['feature_name'],
                'secucode': result['secucode'], 'report_date': result['report_date'],
                'recorded_value': before, 'replayed_value': after,
                'recorded_reason': result['not_computable_reason'], 'replayed_reason': calculated['not_computable_reason'],
                'match': bool(value_matches and reason_matches),
                'replay_scope': 'saved-inputs/current-code', 'recorded_code': original, 'current_code': current,
                'code_changed_or_unverified': original.get('commit') != current['commit'] or
                original.get('dirty') is not False or current['dirty'] is not False,
                'input_version_ids': json.loads(result['input_version_ids'])}
    finally:
        if owns_transaction:
            repo.conn.rollback()
