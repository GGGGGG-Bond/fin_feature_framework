"""Explicit, content-bound review declarations, saved with cleaning configuration.

Declarations are human review input, never inferred vendor metadata. No declaration
means the existing unknown-unit/comparability rules continue to apply.
"""
import hashlib
import json

from fin_feature.storage.hashing import canonical_json, is_valid_hash


def raw_digest(raw):
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


def validate_reviews(entries):
    from fin_feature.features.standardize import FIELD_UNITS
    from fin_feature.utils.date_utils import normalize_date
    if not isinstance(entries, list):
        raise ValueError('reviewed_inputs must be a list')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('reviewed input must be an object')
        key = tuple(entry.get(k) for k in ('secucode', 'report_date', 'table_type'))
        if not all(isinstance(k, str) and k for k in key) or key in seen:
            raise ValueError('reviewed input identity missing or duplicated')
        seen.add(key)
        if key[2] not in ('income_statement', 'balance_sheet', 'financial_indicator') or normalize_date(key[1]) != key[1]:
            raise ValueError('reviewed input has unsupported table/date')
        if any(not is_valid_hash(entry.get(k)) for k in ('raw_digest', 'metric_hash', 'semantic_hash')):
            raise ValueError('reviewed input must bind raw content and standardized metrics')
        if not isinstance(entry.get('evidence_ref'), str) or not entry['evidence_ref'].strip():
            raise ValueError('reviewed input requires an evidence reference')
        for k in ('currency', 'amount_unit', 'scope', 'alignment'):
            if not isinstance(entry.get(k), str) or entry[k] in ('', 'unknown'):
                raise ValueError('reviewed input requires explicit ' + k)
        if entry.get('period_basis') not in ('flow_ytd', 'stock_end'):
            raise ValueError('reviewed input requires an explicit period basis')
        fields = entry.get('fields')
        if not isinstance(fields, dict) or not fields:
            raise ValueError('reviewed input requires field definitions')
        for field, spec in fields.items():
            if not isinstance(spec, dict) or not isinstance(spec.get('definition'), str) or not spec['definition']:
                raise ValueError('reviewed field requires a definition')
            if spec.get('unit') not in ('amount', 'percent', 'times'):
                raise ValueError('unsupported reviewed field unit')
            if field not in FIELD_UNITS or (field != 'current_ratio' and spec['unit'] != FIELD_UNITS[field]):
                raise ValueError('reviewed field unit conflicts with the standard field definition')
            # Only this previously unknown field needs an explicit normalization.
            if field == 'current_ratio' and spec['unit'] != 'times':
                raise ValueError('reviewed current_ratio must explicitly use times')
    return json.loads(canonical_json(entries))


def review_for(version, config):
    for entry in config.get('reviewed_inputs', []):
        if all(entry[k] == version[k] for k in ('secucode', 'report_date', 'table_type')):
            if (entry['raw_digest'] != raw_digest(json.loads(version['raw_fields'] or '{}'))
                    or entry['metric_hash'] != version['metric_hash']
                    or entry['semantic_hash'] != version['semantic_hash']):
                raise ValueError('reviewed input content changed; a new review is required')
            return entry
    return None


def comparison_bases(version, config):
    entry = review_for(version, config)
    if entry is None:
        return {}
    return {field: canonical_json({
        **{k: entry[k] for k in ('currency', 'amount_unit', 'scope', 'alignment', 'period_basis')},
        'definition': spec['definition'], 'unit': spec['unit'],
    }) for field, spec in entry['fields'].items()}
