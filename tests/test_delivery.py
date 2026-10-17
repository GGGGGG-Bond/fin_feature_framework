"""Delivery checks exercise real evidence and immutable lineage in temporary DBs."""
import csv
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from examples.offline_flow import run_demo
from fin_feature.delivery import build_report, export_report, cmd_delivery_report
from fin_feature.storage.database import get_connection
from fin_feature.storage.repository import Repository
from fin_feature.storage.raw_store import RawStore


@pytest.fixture
def delivered(tmp_path):
    root=tmp_path/'demo';run_demo(root)
    conn=get_connection(str(root/'finance.db'))
    repo=Repository(conn);store=RawStore(root/'raw',readonly=True)
    yield root,repo,store
    conn.close()


def test_report_includes_plan_denominator_and_exact_lineage(delivered,tmp_path):
    root,repo,store=delivered
    before=repo.conn.total_changes
    report=export_report(repo,store,tmp_path/'report',review_summary='docs/stage-3/review-result.json')
    assert report['exit_code']==0
    assert report['expected_features']==report['returned_features']==24
    assert 0<report['non_null_features']<24
    assert report['feature_value_coverage']==report['non_null_features']/24
    assert report['run_status_counts']=={'success':1}
    assert report['unit_status_counts']=={'success':3}
    assert report['sources'][0]['versions']==6
    assert report['external_review']['planned_pairs']==96
    assert report['external_review']['reference_match_pairs']==32
    assert 'not revalidated' in report['external_review']['verification_scope']
    assert report['audit']['clean']
    assert report['not_computable_reasons']['incomparable_basis']>0
    assert repo.conn.total_changes==before
    rows=list(csv.DictReader((tmp_path/'report/features.csv').open(encoding='utf-8-sig')))
    assert len(rows)==24 and all(r['compute_run_id'] for r in rows)
    versions=json.loads((tmp_path/'report/versions.json').read_text())
    assert len(versions)==6
    assert all(v['source_observations'] and v['source_observations'][0]['raw_content_hash'] for v in versions)
    inputs=json.loads((tmp_path/'report/inputs.json').read_text())
    declared={v['version_id'] for v in versions}
    assert all(set(i['input_version_ids'])<=declared for i in inputs)


def test_missing_output_does_not_shrink_feature_denominator(delivered):
    _,repo,store=delivered
    repo.conn.execute("DELETE FROM features WHERE feature_name='gross_margin_feat'");repo.commit()
    report,*_=build_report(repo,store)
    assert report['expected_features']==24 and report['returned_features']==22
    assert report['missing_features']==2 and report['exit_code']==1
    assert report['issue_counts']['missing_current_features']==1


@pytest.mark.parametrize('field,value,reason',[
    ('feature_value',99,'stale_feature_value'),
    ('input_version_ids','[]','stale_feature_inputs'),
    ('config_hash','outdated','stale_feature_inputs')])
def test_stale_or_tampered_feature_is_reported(delivered,field,value,reason):
    _,repo,store=delivered
    repo.conn.execute(f"UPDATE features SET {field}=? WHERE feature_name='gross_margin_feat'",(value,));repo.commit()
    report,*_=build_report(repo,store)
    assert report['exit_code']==1 and report['issue_counts'][reason]==2


def test_missing_raw_file_and_failed_run_are_reported(delivered):
    _,repo,store=delivered
    snapshot=repo.conn.execute('SELECT file_path FROM raw_snapshot LIMIT 1').fetchone()[0]
    (store.root/snapshot).unlink()
    with repo.unit_of_work():
        run=repo.create_ingest_run('eastmoney',request_scope=json.dumps({'secucodes':['000001.SZ']}))
        repo.finish_ingest_run(run,'failed',failed_units=1)
    report,*_=build_report(repo,store)
    assert report['exit_code']==1
    assert report['run_status_counts']['failed']==1
    assert 'raw_evidence_audit_failed' in report['issue_counts']
    assert 'ingest_run_not_success' in report['issue_counts']


def test_report_refuses_evidence_tree_and_existing_output(delivered,tmp_path):
    _,repo,store=delivered
    with pytest.raises(ValueError,match='outside raw'):
        export_report(repo,store,store.root/'report')
    assert not (store.root/'report').exists()
    target=tmp_path/'existing';target.mkdir();(target/'keep').write_text('user data')
    with pytest.raises(FileExistsError):export_report(repo,store,target)
    assert (target/'keep').read_text()=='user data'
    assert list(target.iterdir())==[target/'keep']


def test_report_empty_database_is_not_full_coverage(repo,tmp_path):
    store=RawStore(tmp_path/'raw')
    result,*_=build_report(repo,store)
    assert result['expected_features']==0 and result['feature_value_coverage'] is None
    assert result['exit_code']==1 and result['issue_counts']['empty_primary_scope']==1


def test_unverified_legacy_inputs_remain_report_issue(repo,tmp_path):
    from test_versioned_pipeline import seed
    from fin_feature.refresh import refresh_current
    seed(repo);refresh_current(repo)
    result,*_=build_report(repo,RawStore(tmp_path/'raw'))
    assert result['exit_code']==1
    assert result['issue_counts']['unverified_current_inputs']==1
    assert result['sources'][0]['evidence_sufficient_versions']==0


def test_report_does_not_commit_outer_transaction(delivered):
    _,repo,store=delivered
    repo.conn.execute('BEGIN')
    result,*_=build_report(repo,store)
    assert result['read_only'] and repo.conn.in_transaction
    repo.rollback()


@pytest.mark.parametrize('mutation',[
    {'schema':'wrong'}, {'planned_pairs':0}, {'numeric_pairs':100},
    {'reference_match_pairs':True}, {'reference_checked_pairs':-1},
    {'counts':{'match_reference':32}}])
def test_invalid_external_review_report_is_rejected(delivered,tmp_path,mutation):
    _,repo,store=delivered
    payload=json.loads(Path('docs/stage-3/review-result.json').read_bytes());payload.update(mutation)
    supplied=tmp_path/'invalid.json';supplied.write_text(json.dumps(payload))
    with pytest.raises(ValueError):export_report(repo,store,tmp_path/'output',review_summary=supplied)
    assert not (tmp_path/'output').exists()


def test_cli_report_readonly_and_output_exit(delivered,tmp_path,capsys):
    root,repo,_=delivered
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'database':{'path':str(root/'finance.db')}}))
    before=repo.conn.total_changes
    assert cmd_delivery_report(Namespace(config=str(cfg),output_dir=str(tmp_path/'report'),review_summary=None))==0
    result=json.loads(capsys.readouterr().out)
    assert result['expected_features']==24
    assert repo.conn.total_changes==before
    absent=tmp_path/'absent.db';cfg.write_text(json.dumps({'database':{'path':str(absent)}}))
    assert cmd_delivery_report(Namespace(config=str(cfg),output_dir=str(tmp_path/'no-report'),review_summary=None))==2
    assert not absent.exists() and not (tmp_path/'no-report').exists()


def test_public_fact_demo_is_portable_and_not_vendor_raw(tmp_path,monkeypatch):
    import requests
    from examples.public_flow import run_public_demo,FIXTURE
    def forbidden(*args,**kwargs):raise AssertionError('offline public demo attempted network')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    fixture=json.loads(FIXTURE.read_bytes())
    assert len(fixture['records'])==32 and fixture['provider_payload'] is False
    for document in fixture['documents']:
        body=Path(document['reference_file']).read_bytes()
        assert hashlib.sha256(body).hexdigest()==document['reference_sha256']
        reference=json.loads(body)
        frozen=[r for r in fixture['records'] if r['document_sha256']==document['sha256']]
        assert len(frozen)==len(reference['records'])
        assert {(r['secucode'],r['report_date'],r['field'],r['value']) for r in frozen}=={
            (r['secucode'],r['report_date'],r['field'],r['value']) for r in reference['records']}
    result=run_public_demo(tmp_path/'public')
    assert result['public_facts'] and result['reconstructed_api_envelopes']
    assert result['original_vendor_responses'] is False and result['network_requests']==0
    assert result['financial_units']==6
    assert result['features']==result['replay_count']==result['replay_matches']==72
    assert result['non_null_features']==16
    assert result['report_exit_code']==0 and result['evidence_audit_clean']
    conn=get_connection(str(tmp_path/'public/finance.db'))
    loss=conn.execute("SELECT feature_value FROM features WHERE secucode='000002.SZ' AND report_date='2024-12-31' AND feature_name='net_profit_margin'").fetchone()[0]
    assert loss<0
    conn.close()


@pytest.mark.parametrize('kind',['amount','currency','link'])
def test_public_fixture_tampering_is_rejected_before_output(tmp_path,monkeypatch,kind):
    from examples import public_flow
    payload=json.loads(public_flow.FIXTURE.read_bytes())
    if kind=='amount':payload['records'][0]['value']='999999'
    elif kind=='currency':payload['currency']='USD'
    else:payload['records'][0]['document_url']='https://example.test/wrong.pdf'
    altered=tmp_path/'altered.json';altered.write_text(json.dumps(payload))
    monkeypatch.setattr(public_flow,'FIXTURE',altered)
    with pytest.raises(ValueError,match='differ'):
        public_flow.run_public_demo(tmp_path/'must-not-exist')
    assert not (tmp_path/'must-not-exist').exists()


def test_real_restatement_fixture_has_before_after_and_unknown_timing():
    reference=json.loads(Path('examples/references/sany-2024-reviewed.json').read_bytes())
    case=json.loads(Path('examples/references/sany-revision-case.json').read_bytes())
    assert case['document']==reference['document'] and case['page']==5
    assert case['original_disclosure_date'] is case['revision_effective_date'] is case['system_switch_time'] is None
    rows={r['field']:r for r in reference['records']}
    assert len(case['changes'])==2
    for change in case['changes']:
        assert change['before']!=change['after']
        assert rows[change['field']]['value']==change['after']
        assert rows[change['field']]['column_label']=='2023年 调整后'
    snapshot=json.loads(Path('docs/stage-3/review-result-20261018.json').read_bytes())
    assert snapshot['planned_pairs']==96 and snapshot['numeric_pairs']==84
    assert snapshot['reference_checked_pairs']==snapshot['reference_match_pairs']==34
    assert snapshot['counts']=={'match_reference':34,'unreviewed':50,'source_missing':12}
