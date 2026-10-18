"""Required-feature demonstration from pinned, reviewed issuer-report facts.

API envelopes are reconstructed fixtures. The ordinary ingestion, cleaning,
feature storage, evidence audit and frozen-input replay paths are exercised.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

import requests

from fin_feature.delivery import export_report
from fin_feature.features.reviewed import raw_digest
from fin_feature.features.standardize import standardize
from fin_feature.features.base import Feature
from fin_feature.features.versioned import DEPENDENCIES
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, REPORT_TO_TABLE_TYPE
from fin_feature.refresh import refresh_current
from fin_feature.replay import replay_result
from fin_feature.storage.database import Database
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository
from fin_feature.storage.hashing import canonical_json

FIXTURE = Path(__file__).with_name('interview_financials.json')
FIXTURE_HASH = '421f9ac3e12f749f3850d449b593884e085a82e88994ce51e123f835ebfc3c41'
API_FIELDS = dict(total_operate_income='TOTAL_OPERATE_INCOME', parent_netprofit='PARENT_NETPROFIT',
                  total_assets='TOTAL_ASSETS', total_liabilities='TOTAL_LIABILITIES',
                  total_equity='TOTAL_EQUITY', current_ratio='CURRENT_RATIO',
                  weightavg_roe='WEIGHTAVG_ROE', gross_margin='XSMLL')


def load_facts(path=FIXTURE, expected_hash=FIXTURE_HASH):
    fixture = json.loads(Path(path).read_bytes())
    # Git normalizes text line endings; bind financial content, not CRLF bytes.
    if hashlib.sha256(canonical_json(fixture).encode()).hexdigest() != expected_hash:
        raise ValueError('reviewed interview fixture changed; review facts and update its identity')
    return fixture


def fixture_rows(fixture):
    facts = {}
    for item in fixture['facts']:
        facts.setdefault(item['report_date'], {})[item['field']] = float(item['value'])
    result = {}
    for date, values in facts.items():
        income = {k: values[k] for k in ('total_operate_income', 'parent_netprofit')}
        result[date, 'income_statement'] = income
        if 'total_assets' in values:
            balance = {k: values[k] for k in ('total_assets', 'total_liabilities', 'total_equity')}
            if 'current_assets' in values:
                balance['current_ratio'] = values['current_assets'] / values['current_liabilities']
            result[date, 'balance_sheet'] = balance
        if 'weightavg_roe' in values:
            indicator = {**income, 'weightavg_roe': values['weightavg_roe']}
            if 'operating_revenue' in values:
                indicator['gross_margin'] = (1 - values['operate_cost'] / values['operating_revenue']) * 100
            result[date, 'financial_indicator'] = indicator
    return result


class ReviewedFactSession:
    def __init__(self, fixture):
        self.company = fixture['secucode']
        self.rows = fixture_rows(fixture)

    def get(self, url, *, params, timeout):
        company = re.search(r'SECUCODE="([0-9]{6}\.(?:SH|SZ))"', params['filter'])
        if company is None or company.group(1) != self.company:
            raise ValueError('reviewed fixture requires its explicit company')
        table = REPORT_TO_TABLE_TYPE[params['reportName']]
        rows = []
        for (date, kind), values in sorted(self.rows.items(), reverse=True):
            if table != kind:
                continue
            row = dict(SECUCODE=self.company, SECURITY_CODE=self.company[:6],
                       SECURITY_NAME_ABBR='RECONSTRUCTED_REVIEWED_FACTS', NOTICE_DATE=None)
            row['REPORTDATE' if table == 'financial_indicator' else 'REPORT_DATE'] = date
            row.update({API_FIELDS[k]: value for k, value in values.items()})
            rows.append(row)
        response = requests.Response()
        response.status_code = 200
        response.encoding = 'utf-8'
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(dict(success=True, result=dict(pages=1, count=len(rows), data=rows))).encode()
        return response


def reviewed_config(repo, fixture, fixture_hash=FIXTURE_HASH):
    definitions = {name: name + '/reviewed-consolidated-report' for name in API_FIELDS}
    definitions.update(gross_margin='(operating_revenue-operating_cost)/operating_revenue',
                       current_ratio='current_assets/current_liabilities')
    reviews = []
    for row in repo.conn.execute('SELECT * FROM statement_version WHERE is_superseded=0'):
        raw = json.loads(row['raw_fields'])
        fields = {field: dict(definition=definitions[field],
                             unit='times' if field == 'current_ratio' else
                                  'percent' if field in ('weightavg_roe', 'gross_margin') else 'amount')
                  for field in API_FIELDS if raw.get(field) is not None}
        reviews.append(dict(secucode=row['secucode'], report_date=row['report_date'], table_type=row['table_type'],
                            raw_digest=raw_digest(raw), metric_hash=row['metric_hash'],
                            semantic_hash=row['semantic_hash'],
                            evidence_ref=fixture.get('review_reference', 'examples/interview_financials.json') + '#canonical-sha256=' + fixture_hash,
                            currency=fixture['currency'], amount_unit=fixture['amount_unit'], scope=fixture['scope'],
                            alignment=fixture.get('alignment', 'moutai-2024-pinned-reported-basis'),
                            period_basis='stock_end' if row['table_type'] == 'balance_sheet' else 'flow_ytd',
                            fields=fields))
    return {'reviewed_inputs': reviews}


def boundary_cases():
    missing = Feature.pct_change_with_reason(120, None)
    negative = Feature.pct_change_with_reason(20, -10)
    unknown = standardize('current_ratio', 200, unit_map={'current_ratio': 'unknown'})
    return [dict(case=name, input_kind='artificial rule example', value=result[0], reason=result[1])
            for name, result in [('missing_comparison', missing), ('negative_base', negative), ('unknown_unit', unknown)]]


def run_interview_demo(output, fixture_path=FIXTURE, expected_hash=FIXTURE_HASH):
    fixture = load_facts(fixture_path, expected_hash)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    db = Database(str(output / 'finance.db'))
    try:
        db.init()
        store = RawStore(output / 'raw')
        repo = Repository(db.conn, raw_store=store)
        executor = EvidenceRequestExecutor(repository=repo, raw_store=store, session=ReviewedFactSession(fixture),
            url='https://offline.invalid/reviewed-interview-facts', max_retries=1)
        run = FinancialIngestionCoordinator(repo, store, executor, recent_periods=4).run(
            [FinancialUnitSpec(fixture['secucode'], report) for report in REPORT_TO_TABLE_TYPE])
        if run.exit_code:
            raise RuntimeError('reviewed fixture ingestion failed')
        cleaning = reviewed_config(repo, fixture, expected_hash)
        config = {'database': {'path': str((output / 'finance.db').resolve())},
                  'raw_store': {'root': str((output / 'raw').resolve())}, 'cleaning': cleaning}
        (output / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        refresh_current(repo, cleaning)
        report = export_report(repo, store, output / 'report', config=cleaning)
        target = [dict(r) for r in repo.conn.execute(
            'SELECT feature_name,feature_value,not_computable_reason FROM features WHERE report_date=? ORDER BY feature_name',
            (fixture['target_period'],))]
        if {r['feature_name'] for r in target if r['feature_value'] is not None} != set(DEPENDENCIES):
            raise RuntimeError('required-feature demonstration is incomplete')
        replays = [replay_result(repo, r[0]) for r in repo.conn.execute('SELECT result_id FROM feature_compute_log ORDER BY rowid')]
        summary = dict(schema='interview-demo-v1', public_facts=True, reconstructed_api_envelopes=True,
            original_vendor_responses=False, network_requests=0, reviewed_public_facts=len(fixture['facts']),
            target_company=fixture['secucode'], target_period=fixture['target_period'], target_features=target,
            required_features=12, demonstrated_features=len(target), features=report['returned_features'],
            non_null_features=report['non_null_features'], not_computable_reasons=report['not_computable_reasons'],
            replay_count=len(replays), replay_matches=sum(r['match'] for r in replays),
            report_exit_code=report['exit_code'], evidence_audit_clean=report['audit']['clean'],
            boundary_cases=boundary_cases(), limitations=fixture['limitations'])
        if report['exit_code'] or not all(r['match'] for r in replays):
            raise RuntimeError('interview evidence audit or replay failed')
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (output / 'fixture-provenance.json').write_bytes(Path(fixture_path).read_bytes())
        (output / 'replay.json').write_text(json.dumps(replays, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return summary
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    result = run_interview_demo(parser.parse_args().output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
