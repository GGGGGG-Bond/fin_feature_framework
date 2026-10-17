from argparse import Namespace
import hashlib
import json

import pytest

from fin_feature import cli
from fin_feature.replay import replay_result
from test_versioned_pipeline import seed, clean, compute, rows


def find(repo, name='gross_margin_feat'):
    return repo.conn.execute('SELECT * FROM feature_compute_log WHERE feature_name=? ORDER BY rowid LIMIT 1',(name,)).fetchone()


def test_offline_flow_has_real_evidence_and_replays_all_results(tmp_path, monkeypatch):
    import requests
    from examples.offline_flow import run_demo
    def forbidden(*args,**kwargs):
        raise AssertionError('offline flow must never use network')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    output=tmp_path/'offline'
    summary=run_demo(output)
    assert summary['artificial_fixture'] is True
    assert summary['network_requests']==0
    assert summary['financial_units']==3 and summary['features']==24
    assert summary['replay_count']==summary['replay_matches']==24
    assert summary['audit_exit_code']==0
    assert len(list((output/'raw').rglob('*.body.gz')))==3
    assert json.loads((output/'summary.json').read_text())==summary


def test_replay_uses_frozen_inputs_after_revisions_and_rule_changes(repo):
    seed(repo);seed(repo,date='2024-03-31')
    clean(repo);compute(repo)
    history=rows(repo,'feature_compute_log')
    seed(repo,gross_margin=80,weightavg_roe=30)
    clean(repo, {'missing_strategy':{'gross_margin':'zero'}})
    # Current projections are irrelevant to replay of historical inputs.
    with repo.unit_of_work():
        repo.conn.execute('DELETE FROM clean_financial_indicator')
        repo.conn.execute('DELETE FROM clean_selection')
    replays=[replay_result(repo,r['result_id']) for r in history]
    assert all(r['match'] for r in replays)
    assert any(r['replayed_reason']=='incomparable_basis' for r in replays)
    assert rows(repo,'feature_compute_log')==history
    assert {r['replay_scope'] for r in replays}=={'saved-inputs/current-code'}


def test_replay_cli_is_readonly_and_does_not_create_run_or_results(repo, db, tmp_path, capsys):
    seed(repo);clean(repo);compute(repo)
    result=find(repo)
    cfg=tmp_path/'replay.yaml'
    cfg.write_text(json.dumps({'database':{'path':db.db_path}}))
    before={t:rows(repo,t) for t in ('features','feature_compute_log','ingest_run')}
    digest=hashlib.sha256(open(db.db_path,'rb').read()).hexdigest()
    code=cli.cmd_replay(Namespace(config=str(cfg),result_id=None,compute_run_id=result['compute_run_id']))
    assert code==0
    output=json.loads(capsys.readouterr().out)
    assert output['total']==output['matched']==12
    assert {t:rows(repo,t) for t in before}==before
    assert hashlib.sha256(open(db.db_path,'rb').read()).hexdigest()==digest


def test_replay_cli_value_difference_returns_failure(repo, db, tmp_path, monkeypatch):
    from fin_feature.features.profitability import GrossMarginFeature
    seed(repo);clean(repo);compute(repo)
    result=find(repo)
    monkeypatch.setattr(GrossMarginFeature,'calculate',lambda self,ctx:0.99)
    cfg=tmp_path/'config.yaml';cfg.write_text(json.dumps({'database':{'path':db.db_path}}))
    assert cli.cmd_replay(Namespace(config=str(cfg),result_id=result['result_id'],compute_run_id=None))==1
    assert find(repo)['feature_value']==0.4


@pytest.mark.parametrize('corrupt',['clean','lineage','legacy'])
def test_replay_rejects_missing_or_inconsistent_saved_inputs(repo, corrupt):
    seed(repo);clean(repo);compute(repo)
    result=find(repo)
    with repo.unit_of_work():
        if corrupt=='clean':
            repo.conn.execute("UPDATE clean_statement SET standardized_fields='{}'")
        else:
            snapshot=json.loads(result['config_snapshot'])
            if corrupt=='lineage':
                snapshot['inputs']=[]
            else:
                snapshot['pipeline_version']='legacy'
            repo.conn.execute('UPDATE feature_compute_log SET config_snapshot=? WHERE result_id=?',
                              (json.dumps(snapshot),result['result_id']))
    with pytest.raises(ValueError):
        replay_result(repo,result['result_id'])


def test_replay_unknown_result_and_missing_database_have_clear_failure(repo, tmp_path):
    with pytest.raises(ValueError,match='does not exist'):
        replay_result(repo,'missing')
    path=tmp_path/'does-not-exist.db'
    cfg=tmp_path/'config.yaml';cfg.write_text(json.dumps({'database':{'path':str(path)}}))
    assert cli.cmd_replay(Namespace(config=str(cfg),result_id='missing',compute_run_id=None))==2
    assert not path.exists()


def test_offline_demo_refuses_existing_directory(tmp_path):
    from examples.offline_flow import run_demo
    with pytest.raises(FileExistsError):
        run_demo(tmp_path)
    assert not (tmp_path/'finance.db').exists()
