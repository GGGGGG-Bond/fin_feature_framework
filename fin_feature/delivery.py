"""Consistent read-only delivery report; fixed coverage denominators and lineage."""
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from fin_feature.cleaning.versioned import VersionedCleanRepository, current_versions
from fin_feature.features.versioned import DEPENDENCIES, evaluate_feature, code_identity
from fin_feature.storage.raw_audit import audit_raw

SCOPE_KEYS = {'report_names','secucodes','recent_periods','mapping_version',
              'max_pages_per_unit','max_body_bytes','stock_count'}


def _review(path):
    if path is None:
        return None
    body = Path(path).read_bytes()
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get('schema') != 'reference-review-summary-v1':
        raise ValueError('review summary must use reference-review-summary-v1')
    keys = ('planned_pairs','numeric_pairs','reference_checked_pairs','reference_match_pairs')
    values = [payload.get(k) for k in keys]
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError('review coverage counts must be non-negative integers')
    planned, numeric, checked, matched = values
    if not planned or numeric > planned or matched > checked or checked > planned or matched > numeric:
        raise ValueError('inconsistent review coverage counts')
    counts = payload.get('counts')
    if (not isinstance(counts, dict) or any(type(v) is not int or v < 0 for v in counts.values())
            or sum(counts.values()) != planned or counts.get('match_reference', 0) != matched):
        raise ValueError('review status counts disagree with fixed denominator')
    return dict(sha256=hashlib.sha256(body).hexdigest(), schema=payload['schema'],
                **dict(zip(keys, values)), counts=payload.get('counts', {}),
                supplied_snapshot_date=payload.get('snapshot_date'),
                verification_scope='supplied summary only; source/PDF evidence was not revalidated by delivery-report')


def build_report(repo, raw_store, config=None, review_summary=None):
    """No migrations or writes. Capture database facts in one read transaction."""
    review = _review(review_summary)
    owns = not repo.conn.in_transaction
    if owns:
        repo.conn.execute('BEGIN')
    try:
        issues = []
        runs = []
        for row in repo.conn.execute('SELECT * FROM ingest_run ORDER BY started_at,run_id'):
            item = dict(row)
            scope = json.loads(item['request_scope'] or '{}')
            item['request_scope'] = {key: value for key, value in scope.items() if key in SCOPE_KEYS}
            runs.append(item)
        units = [dict(r) for r in repo.conn.execute('SELECT * FROM ingest_unit ORDER BY run_id,unit_id')]
        run_status = dict(Counter(r['status'] for r in runs))
        unit_status = dict(Counter(u['status'] for u in units))
        for run in runs:
            if run['status'] != 'success':
                issues.append(dict(reason_code='ingest_run_not_success', run_id=run['run_id'], status=run['status']))
        versions = current_versions(repo)
        unverified = sum(not version['evidence_sufficient'] for version in versions)
        if unverified:
            issues.append(dict(reason_code='unverified_current_inputs', count=unverified))
        for version in versions:
            version['source_observations'] = [dict(r) for r in repo.conn.execute(
                'SELECT o.observation_id,o.snapshot_id,o.run_id,o.row_index,o.observed_at,'
                's.raw_content_hash,s.file_path,s.fetched_at FROM statement_observation o '
                'JOIN raw_snapshot s ON s.snapshot_id=o.snapshot_id WHERE o.version_id=? ORDER BY o.observed_at,o.observation_id',
                (version['version_id'],))]
        unit_scopes = []
        allowed = {'endpoint','report_name','columns','filters','start_date','end_date','sort_columns',
                   'sort_types','page_size','recent_periods','mapping_version','stock_count'}
        for unit in units:
            scope = json.loads(unit['request_scope'] or '{}')
            unit_scopes.append(dict(unit_id=unit['unit_id'],run_id=unit['run_id'],
                scope={key:value for key,value in scope.items() if key in allowed}))
        source_counts = [dict(r) for r in repo.conn.execute(
            'SELECT source_name,COUNT(*) AS versions,SUM(is_superseded=0) AS current_versions,'
            'SUM(evidence_sufficient=1) AS evidence_sufficient_versions FROM statement_version GROUP BY source_name')]
        primary_pairs = {(r['secucode'],r['report_date']) for r in versions}
        expected = len(primary_pairs) * len(DEPENDENCIES)
        features = [dict(r) for r in repo.conn.execute(
            "SELECT * FROM features WHERE definition_version='stage1-v1' ORDER BY secucode,report_date,feature_name")]
        adapter = None
        try:
            adapter = VersionedCleanRepository(repo, config)
        except ValueError as exc:
            issues.append(dict(reason_code='stale_clean', detail=str(exc)))
        indexed = {(r['secucode'],r['report_date'],r['feature_name']):r for r in features}
        declared = {(c,p,name) for c,p in primary_pairs for name in DEPENDENCIES}
        missing = declared - indexed.keys()
        extra = indexed.keys() - declared
        if missing:
            issues.append(dict(reason_code='missing_current_features', count=len(missing)))
        if extra:
            issues.append(dict(reason_code='extra_current_features', count=len(extra)))
        lineage = []
        for key, feature in sorted(indexed.items()):
            ids = json.loads(feature['input_version_ids'] or '[]')
            lineage.append(dict(secucode=key[0],report_date=key[1],feature_name=key[2],
                                compute_run_id=feature['compute_run_id'],input_version_ids=ids,
                                definition_version=feature['definition_version'],config_hash=feature['config_hash']))
            if adapter is None or key not in declared:
                continue
            calculated = evaluate_feature(adapter, *key)
            expected_ids = sorted({i['record_ref'] for i in calculated['inputs'] if i['record_ref']})
            if sorted(ids) != expected_ids or feature['config_hash'] != adapter.config_hash:
                issues.append(dict(reason_code='stale_feature_inputs', secucode=key[0],report_date=key[1],feature_name=key[2]))
            actual, desired = feature['feature_value'], calculated['feature_value']
            match = actual is None and desired is None
            if actual is not None and desired is not None:
                match = math.isfinite(actual) and math.isclose(actual,desired,rel_tol=1e-12,abs_tol=1e-12)
            if not match or feature['not_computable_reason'] != calculated['not_computable_reason']:
                issues.append(dict(reason_code='stale_feature_value', secucode=key[0],report_date=key[1],feature_name=key[2]))
        audit = audit_raw(repo.conn, raw_store)
        if audit.exit_code():
            issues.append(dict(reason_code='raw_evidence_audit_failed', errors=len(audit.errors)))
        if not expected:
            issues.append(dict(reason_code='empty_primary_scope'))
        populated = sum(r['feature_value'] is not None for k,r in indexed.items() if k in declared)
        reasons = dict(Counter(r['not_computable_reason'] or 'unspecified_null' for r in features if r['feature_value'] is None))
        reason_count = dict(Counter(d['reason_code'] for d in issues))
        summary = dict(schema='delivery-report-v1', code_identity=code_identity(), read_only=True,
            request_runs=runs, unit_request_scopes=unit_scopes, run_status_counts=run_status, unit_status_counts=unit_status,
            failed_units=[dict(unit_id=u['unit_id'],run_id=u['run_id'],status=u['status'],failure_code=u['failure_code'])
                          for u in units if u['status'] not in ('success','success_empty')],
            sources=source_counts, primary_source='eastmoney', primary_company_periods=len(primary_pairs),
            expected_features=expected, returned_features=len(features), missing_features=len(missing),
            non_null_features=populated, feature_value_coverage=populated/expected if expected else None,
            not_computable_reasons=reasons, input_version_count=len(versions), immutable_compute_logs=repo.get_table_count('feature_compute_log'),
            clean_current_valid=adapter is not None, audit=audit.as_dict(), external_review=review,
            issues=issues, issue_counts=reason_count, exit_code=1 if issues else 0,
            limitations=['Second provider checks remain separate and never silently enter primary financial values.',
                         'External review report is a supplied snapshot, not fresh evidence verification.',
                         'Unknown units/comparison basis/public dates remain unknown; NULL is not zero.',
                         'Saved-input/current-code replay does not restore the historical software environment.',
                         'Financial future-growth group analysis is not an investment return backtest.'])
        return summary, features, versions, lineage
    finally:
        if owns:
            repo.conn.rollback()


def export_report(repo, raw_store, output_dir, config=None, review_summary=None):
    root = Path(output_dir).expanduser().resolve()
    raw_root = raw_store.root.resolve()
    if root == raw_root or raw_root in root.parents:
        raise ValueError('report directory must be outside raw evidence root')
    result, features, versions, lineage = build_report(repo,raw_store,config,review_summary)
    root.mkdir(parents=True, exist_ok=False)
    for name, payload in [('summary',result),('versions',versions),('inputs',lineage)]:
        (root/(name+'.json')).write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    columns = ['secucode','report_date','feature_name','definition_version','feature_value',
               'not_computable_reason','compute_run_id','input_version_ids','config_hash']
    with (root/'features.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore')
        writer.writeheader();writer.writerows(features)
    lines = ['# 数据与特征交付报告','',
             f"计划特征 {result['expected_features']}；返回 {result['returned_features']}；有值 {result['non_null_features']}；缺输出 {result['missing_features']}。",
             f"当前主来源公司/报告期 {result['primary_company_periods']}；当前输入版本 {result['input_version_count']}。",
             '', '## 采集状态','',json.dumps(result['run_status_counts'],ensure_ascii=False),
             '', '## 特征不可计算原因','',json.dumps(result['not_computable_reasons'],ensure_ascii=False),
             '', '## 检查发现','',json.dumps(result['issue_counts'],ensure_ascii=False),
             '', '## 外部双源核查快照','',json.dumps(result['external_review'],ensure_ascii=False),
             '', '## 能力边界',''] + ['- '+text for text in result['limitations']]
    (root/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def cmd_delivery_report(args):
    import sys
    import sqlite3
    from fin_feature.config import load_config
    from fin_feature.storage.database import get_readonly_connection
    from fin_feature.storage.raw_store import build_readonly_raw_store, RawStoreError
    from fin_feature.storage.repository import Repository
    conn = None
    try:
        config = load_config(args.config)
        conn = get_readonly_connection(config.db_path)
        store = build_readonly_raw_store(config, config.db_path)
        result = export_report(Repository(conn),store,args.output_dir,config.cleaning,args.review_summary)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return result['exit_code']
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error,RawStoreError) as exc:
        print(f'Delivery report failed: {type(exc).__name__}: {exc}',file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
