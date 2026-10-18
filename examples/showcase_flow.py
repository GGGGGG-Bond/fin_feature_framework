"""Offline showcase: captured field extracts, two-source checks and price returns.

Selected extracts are real captures; the HTTP envelopes below are reconstructed.
No network requests are made. Original capture body hashes identify parent evidence,
not the reconstructed envelopes written by this demo.
"""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import requests

from fin_feature.addons.returns import run_return_backtest, export_return_backtest
from fin_feature.addons.return_study import run_return_study, export_return_study
from fin_feature.delivery import export_report
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec
from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.storage.database import Database
from fin_feature.storage.hashing import canonical_json
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository

SNAPSHOTS = Path(__file__).with_name('snapshots')
IDENTITIES = {
    'dual-source.json': 'e98f37d814f36e58c7a461ea189e61e968af105e116c1d19eb87de5428a0cf9a',
    'financials.json': 'fb87782e458bcf4ec72275747b90767300500b520cfa38c7f7f90e2cc7d612eb',
    'official-reviews.json': '56c113cfaadaaf334f5e0e56f08a4d0717e6627a2e2032f2e415b6c371ac5e87',
    'prices.json': '21fcd7a3952245add6040b2cb6d61c9c846ebbad8614cbb2267e7eb2355e5d56',
}


def load_snapshots(root=SNAPSHOTS):
    result = {}
    manifest = json.loads((Path(root) / 'manifest.json').read_bytes())
    if manifest.get('files') != IDENTITIES:
        raise ValueError('snapshot manifest changed; review captured facts before updating identities')
    for name, expected in IDENTITIES.items():
        item = json.loads((Path(root) / name).read_bytes())
        if hashlib.sha256(canonical_json(item).encode()).hexdigest() != expected:
            raise ValueError(f'captured snapshot identity mismatch: {name}')
        result[name] = item
    return result


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


class SnapshotSession:
    def __init__(self, pages):
        self.pages = {(p['secucode'], p['report_name']): p['rows'] for p in pages}
        if len(self.pages) != len(pages):
            raise ValueError('duplicate snapshot unit')

    def get(self, url, *, params, timeout):
        company = re.search(r'SECUCODE="([0-9]{6}\.(?:SH|SZ))"', params['filter'])
        if company is None or params['pageNumber'] != '1':
            raise ValueError('unexpected offline snapshot request')
        rows = self.pages[company.group(1), params['reportName']]
        response = requests.Response()
        response.status_code, response.encoding = 200, 'utf-8'
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(dict(success=True, result=dict(pages=1, count=len(rows), data=rows)), ensure_ascii=False).encode()
        return response


def run_pipeline(pages, output, *, recent_periods, replay=True):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    db = Database(str(output / 'finance.db'))
    try:
        db.init()
        store = RawStore(output / 'raw')
        repo = Repository(db.conn, raw_store=store)
        executor = EvidenceRequestExecutor(repository=repo, raw_store=store, session=SnapshotSession(pages),
            url='https://offline.invalid/captured-field-extracts-reconstructed', max_retries=1)
        run = FinancialIngestionCoordinator(repo, store, executor, recent_periods=recent_periods).run(
            [FinancialUnitSpec(p['secucode'], p['report_name']) for p in pages])
        if run.exit_code:
            raise RuntimeError('offline captured-fields ingestion failed')
        refresh_current(repo)
        report = export_report(repo, store, output / 'report')
        features = [dict(r) for r in repo.conn.execute('SELECT secucode,report_date,feature_name,feature_value,not_computable_reason FROM features ORDER BY secucode,report_date,feature_name')]
        replays = [replay_result(repo, r[0]) for r in repo.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY rowid')] if replay else []
        if report['exit_code'] or any(not r['match'] for r in replays):
            raise RuntimeError('captured-fields evidence audit/replay failed')
        write_json(output / 'replay.json', replays)
        write_json(output / 'config.json', dict(database=dict(path=str((output / 'finance.db').resolve())), raw_store=dict(root=str((output / 'raw').resolve()))))
        return dict(features=features, report=report, replay_count=len(replays), replay_matches=sum(r['match'] for r in replays))
    finally:
        db.close()


def dual_comparison(capture, reviews):
    """Numeric check does not grant provider semantic/revision comparability."""
    facts = {(r['secucode'], r['report_date'], r['field']): r for r in reviews['facts']}
    documents = {d['id']: d for d in reviews['documents']}
    rows = []
    tolerance = Decimal(reviews['absolute_tolerance_yuan'])
    for company in capture['companies']:
        indices = {source: {(r['report_date'], r['field']): r for r in doc['records']}
                   for source, doc in company['providers'].items()}
        if indices['eastmoney'].keys() != indices['sina'].keys():
            raise ValueError('dual snapshot keys do not cover the same planned positions')
        for (period, field), a in indices['eastmoney'].items():
            b = indices['sina'][period, field]
            av, bv = a.get('value'), b.get('value')
            has_pair = av is not None and bv is not None
            delta = Decimal(str(av)) - Decimal(str(bv)) if has_pair else None
            fact = facts.get((company['secucode'], period, field))
            status = 'missing_or_definition'
            official_a = official_b = None
            if has_pair and fact:
                official_a = Decimal(str(av)) - Decimal(fact['value'])
                official_b = Decimal(str(bv)) - Decimal(fact['value'])
                status = 'matches_report' if abs(official_a) <= tolerance and abs(official_b) <= tolerance else 'differs_from_report'
            elif has_pair:
                status = 'not_reviewed'
            row = dict(secucode=company['secucode'], name=company['name'], group=company['group'], report_date=period,
                field=field, eastmoney=av, sina=bv, numeric_delta=str(delta) if delta is not None else None,
                numeric_agreement=has_pair and abs(delta) <= tolerance, official_status=status,
                official_value=fact['value'] if fact else None, official_delta_eastmoney=str(official_a) if official_a is not None else None,
                official_delta_sina=str(official_b) if official_b is not None else None,
                original_semantics='unknown: unit/currency/scope/revision alignment not jointly proven',
                upstream_independence_verified=False, provider_metadata=a.get('provider_metadata'),
                parent_evidence_refs=[a.get('evidence_ref'), b.get('evidence_ref')], parent_requests=company['requests'])
            if fact:
                row.update(document={**documents[fact['document_id']], 'page': fact['page'], 'row_label': fact['row_label'],
                                     'document_value': fact.get('document_value', fact['value']), 'document_unit': fact.get('document_unit', fact['unit']),
                                     'conversion': fact.get('conversion', 'multiply stated unit')})
            if status == 'differs_from_report' and company['secucode'] == '600031.SH' and period == '2024-12-31':
                later = next((r for r in reviews['later_comparative']['comparative_facts'] if r['field'] == field), None)
                if later:
                    row.update(later_comparative=later, later_document=reviews['later_comparative'],
                        matches_later_comparative=abs(Decimal(str(av))-Decimal(later['value'])) <= tolerance and abs(Decimal(str(bv))-Decimal(later['value'])) <= tolerance)
            rows.append(row)
    return rows


def official_balance_pages(reviews):
    grouped = {}
    for fact in reviews['facts']:
        if fact['report_date'] != '2024-12-31' or fact['table_type'] != 'BS':
            continue
        grouped.setdefault(fact['secucode'], {})[fact['field'].upper()] = float(fact['value'])
    return [dict(secucode=code, report_name='RPT_DMSK_FN_BALANCE', rows=[dict(SECUCODE=code,
                 SECURITY_CODE=code[:6], SECURITY_NAME_ABBR='RECONSTRUCTED_2024_REPORT', REPORT_DATE='2024-12-31',
                 NOTICE_DATE=None, **values)]) for code, values in sorted(grouped.items())]


def run_showcase(output):
    snapshots = load_snapshots()  # Validate everything before creating output.
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    financials, dual, reviews, prices = [snapshots[k] for k in ('financials.json', 'dual-source.json', 'official-reviews.json', 'prices.json')]
    primary = run_pipeline(financials['pages'], output / 'captured', recent_periods=8)
    comparison = dual_comparison(dual, reviews)
    write_json(output / 'dual-source.json', comparison)
    sany_document = next(d for d in reviews['documents'] if d['id'] == 'sany')
    write_json(output / 'disclosure-editions.json', dict(schema='disclosure-editions-v1', secucode='600031.SH',
        report_date='2024-12-31', first_public_at=None, exact_revision_public_at=None,
        editions=[dict(kind='original_2024_annual_edition', document=sany_document, available_by=sany_document['available_by'],
                       values={f['field']: f['value'] for f in reviews['facts'] if f['document_id'] == 'sany' and f['report_date'] == '2024-12-31'}),
                  dict(kind='later_2025_report_comparative', document=reviews['later_comparative'], available_by='2026-03-31',
                       availability_basis='Conservative next-day bound after official HKEX 2026-03-30 document path; not exact first/revision time.',
                       values={f['field']: f['value'] for f in reviews['later_comparative']['comparative_facts']})],
        rule='Select only editions with declared available_by at or before the formation date; unknown first/revision instants remain unknown. Do not replace original report facts with later comparatives.'))
    write_json(output / 'features.json', primary['features'])
    import csv
    fields = ['secucode', 'name', 'report_date', 'field', 'eastmoney', 'sina', 'numeric_delta', 'numeric_agreement', 'official_value', 'official_delta_eastmoney', 'official_status', 'original_semantics', 'upstream_independence_verified']
    with (output / 'dual-source.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(comparison)
    # These are separate dated-report facts. Current 2026 provider revisions must
    # never become historical 2025 signals merely because report_date is 2024.
    signal_run = run_pipeline(official_balance_pages(reviews), output / 'report-signals', recent_periods=1)
    documents = {d['id']: d for d in reviews['documents']}
    facts = {(r['secucode'], r['field']): r for r in reviews['facts'] if r['report_date'] == '2024-12-31'}
    records = []
    for r in signal_run['features']:
        if r['feature_name'] != 'debt_asset_ratio': continue
        fact = facts[r['secucode'], 'total_assets']; doc = documents[fact['document_id']]
        records.append({**r, 'available_at': doc['available_by'], 'evidence_ref': doc['url'], 'availability_basis': doc['availability_basis'],
                        'computed_inputs': 'report-signals/report/inputs.json', 'document_sha256': doc['sha256']})
    signals = dict(schema='return-signals-v1', feature_name='debt_asset_ratio', records=records)
    write_json(output / 'signals.json', signals)
    backtest = run_return_backtest(signals, prices, formation_date='2025-05-05', end_date='2025-12-31')
    export_return_backtest(backtest, output / 'backtest')
    plan = json.loads(Path(__file__).with_name('return-study-plan.json').read_bytes())
    study = run_return_study(signals, prices, windows=plan['windows'], universes=plan['universes'], costs=plan['cost_grid_bps'])
    export_return_study(study, output / 'return-study')
    current = {r['secucode']: r for r in primary['features'] if r['report_date'] == '2024-12-31' and r['feature_name'] == 'debt_asset_ratio'}
    naive = []
    for page in financials['pages']:
        if page['report_name'] != 'RPT_DMSK_FN_BALANCE': continue
        row = next(r for r in page['rows'] if r['REPORT_DATE'].startswith('2024-12-31'))
        naive.append({**current[page['secucode']], 'available_at': row['NOTICE_DATE'][:10] if row.get('NOTICE_DATE') else None,
                      'evidence_ref': 'financials.json#'+page['raw_sha256']})
    rejected = run_return_backtest(dict(schema='return-signals-v1', feature_name='debt_asset_ratio', records=naive), prices,
                                  formation_date='2025-05-05', end_date='2025-12-31')
    write_json(output / 'lookahead-rejections.json', rejected)
    from examples.interview_flow import run_interview_demo
    complete = run_interview_demo(output / 'required-features')
    from examples.verified_flow import run_verified
    verified = run_verified(output / 'reviewed')
    coverage = dict(schema='feature-coverage-v2', provider=dict(positions=len(primary['features']),
        finite=sum(r['feature_value'] is not None for r in primary['features']),
        null_reasons=dict(Counter(r['not_computable_reason'] for r in primary['features'] if r['feature_value'] is None))),
        reviewed=dict(positions=verified['features'], finite=verified['non_null_features'],
                      target_positions=verified['target_positions'], target_finite=verified['target_finite']),
        note='Provider and reviewed issuer input universes remain separate; do not merge denominators or transfer comparability declarations.')
    write_json(output / 'coverage.json', coverage)
    summary = dict(schema='showcase-v1', companies=financials['companies'], periods=8, statement_versions=192,
        captured_at=financials['captured_at'], captured_primary_requests=24, captured_dual_requests=32, captured_price_requests=9,
        network_requests=0, reconstructed_api_envelopes=True, original_responses_in_repository=False,
        features=len(primary['features']), non_null_features=sum(r['feature_value'] is not None for r in primary['features']),
        replay_count=primary['replay_count'], replay_matches=primary['replay_matches'],
        evidence_audit_clean=primary['report']['audit']['clean'], dual_positions=len(comparison),
        dual_statuses=dict(Counter(r['official_status'] for r in comparison)), dual_numeric_agreements=sum(r['numeric_agreement'] for r in comparison),
        backtest_status=backtest['status'], backtest_members=len(backtest['members']), backtest_groups=len(backtest['groups']),
        historical_current_provider_rejected=len(rejected['excluded_signals']), required_features_demonstrated=complete['demonstrated_features'],
        reviewed_companies=3, reviewed_facts=verified['reviewed_facts'], reviewed_target_positions=verified['target_positions'],
        reviewed_target_finite=verified['target_finite'], reviewed_features=verified['features'],
        reviewed_non_null_features=verified['non_null_features'], reviewed_replay_count=verified['replay_count'],
        reviewed_replay_matches=verified['replay_matches'], return_study_scenarios=len(study['scenarios']),
        return_study_status=study['status'],
        limitations=['Eight fixed companies, eight quarters; neither full-market coverage nor an unbiased investment universe.',
                     'Captured field extracts are real, offline HTTP envelopes are reconstructed; parent bodies are outside Git.',
                     'Two websites do not prove independent upstreams or identical revision semantics.',
                     'Unknown provider cross-period basis stays NULL; three companies separately demonstrate 36/36 target results with reviewed issuer facts.',
                     'Backtest uses dated 2024 issuer-report inputs and conservative availability declarations, not archived historical HTTP bytes.'])
    if backtest['exit_code'] or study['exit_code'] or verified['target_finite'] != 36 or len(rejected['excluded_signals']) != 8:
        raise RuntimeError('backtest or availability guard failed')
    write_json(output / 'summary.json', summary)
    write_json(output / 'fixture-provenance.json', dict(manifest=IDENTITIES, financial_capture={k:v for k,v in financials.items() if k!='pages'}, official_reviews=reviews))
    from fin_feature.showcase_report import write_showcase
    write_showcase(output, summary, primary['features'], comparison, backtest, complete, verified=verified, study=study)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    summary = run_showcase(parser.parse_args().output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
