import json
import hashlib
import subprocess
import sys
import pytest
from test_sample_batch import collector,plan
from fin_feature.sample_batch import run_batch
from fin_feature.review_batch import review_batch


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def files(tmp_path,full=False):
    batch=tmp_path/'batch'
    run_batch(plan('600519.SH','000333.SZ'),batch,collector=collector,pause=lambda _:None)
    document=tmp_path/'fixture.pdf';document.write_bytes(b'%PDF-ARTIFICIAL reference')
    entries=[]
    for code in ('600519.SH','000333.SZ') if full else ('600519.SH',):
        source=batch/code
        left=json.loads((source/'eastmoney.json').read_text())
        selected=left['records'] if full else [next(r for r in left['records'] if r['field']=='total_assets')]
        refs=[dict(r,value='-10' if r['field']=='parent_netprofit' else '100',page=1,
                   row_label='ARTIFICIAL '+r['field'],column_label='2024') for r in selected]
        reference=dict(schema='reviewed-reference-v1',coverage='exact' if full else 'selected-fields',
            reviewed_by='ARTIFICIAL fixture reviewer',document=dict(url='https://example.test/doc.pdf',sha256=sha(document)),
            input_sha256=[sha(source/(s+'.json')) for s in ('eastmoney','sina')],currency='CNY',unit='yuan',
            scope='consolidated',source_units={'eastmoney':'yuan','sina':'yuan'},absolute_tolerance='0.01',records=refs)
        refpath=tmp_path/(code+'.reference.json');refpath.write_text(json.dumps(reference))
        entries.append(dict(secucode=code,reference=refpath.name,document=document.name))
    config=tmp_path/'reviews.json';config.write_text(json.dumps(dict(schema='reference-review-plan-v1',reviews=entries)))
    return batch,config,document


def test_subset_preserves_planned_denominator_and_no_network(tmp_path,monkeypatch):
    batch,config,doc=files(tmp_path)
    import requests
    def forbidden(*a,**kw):raise AssertionError('review must remain offline')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    result=review_batch(batch,config,tmp_path/'out')
    assert result['planned_pairs']==12 and result['numeric_pairs']==12
    assert result['reference_checked_pairs']==1 and result['unreviewed_planned_pairs']==11
    assert result['counts']=={'match_reference':1,'unreviewed':11}
    assert result['exit_code']==1 and result['historical_visibility_verified'] is False
    assert (tmp_path/'out/fields.json').exists()


def test_no_reviews_is_not_a_pass(tmp_path):
    batch,cfg,doc=files(tmp_path);cfg.write_text(json.dumps(dict(schema='reference-review-plan-v1',reviews=[])))
    result=review_batch(batch,cfg,tmp_path/'out')
    assert result['reference_checked_pairs']==0 and result['counts']=={'unreviewed':12}
    assert result['exit_code']==1


@pytest.mark.parametrize('damage',['pdf','raw','mapped'])
def test_damaged_evidence_or_reference_is_failure_with_fixed_denominator(tmp_path,damage):
    batch,cfg,doc=files(tmp_path)
    if damage=='pdf':doc.write_bytes(doc.read_bytes()+b'changed')
    elif damage=='raw':next((batch/'600519.SH').rglob('*.gz')).write_bytes(b'corrupt')
    else:
        p=batch/'600519.SH/sina.json';d=json.loads(p.read_text());d['records'][0]['value']='999';p.write_text(json.dumps(d))
    r=review_batch(batch,cfg,tmp_path/'out')
    assert r['planned_pairs']==12 and r['exit_code']==2 and len(r['errors'])==1
    assert sum(r['counts'].values())==12 and r['reference_match_pairs']==0
    assert r['counts']['review_failed' if damage=='pdf' else 'collection_failed']==6


@pytest.mark.parametrize('change',['duplicate','unknown'])
def test_bad_review_scope_rejected_before_output(tmp_path,change):
    batch,cfg,doc=files(tmp_path);d=json.loads(cfg.read_text())
    if change=='duplicate':d['reviews'].append(d['reviews'][0])
    else:d['reviews'][0]['secucode']='000001.SZ'
    cfg.write_text(json.dumps(d))
    with pytest.raises(ValueError):review_batch(batch,cfg,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_real_difference_is_not_changed_to_unreviewed(tmp_path):
    batch,cfg,doc=files(tmp_path);ref=tmp_path/'600519.SH.reference.json'
    d=json.loads(ref.read_text());d['records'][0]['value']='101';ref.write_text(json.dumps(d))
    r=review_batch(batch,cfg,tmp_path/'out')
    assert r['counts']=={'difference_reference':1,'unreviewed':11}
    assert r['reference_checked_pairs']==1 and r['exit_code']==1 and not r['errors']


def test_all_numeric_reference_fields_match_still_no_time_claim(tmp_path):
    batch,cfg,doc=files(tmp_path,full=True)
    r=review_batch(batch,cfg,tmp_path/'out')
    assert r['reference_match_pairs']==12 and r['counts']=={'match_reference':12} and r['exit_code']==0
    assert r['provider_revision_alignment_verified'] is False
    assert r['upstream_independence_verified'] is False


def test_cli_subprocess_and_existing_output(tmp_path):
    batch,cfg,doc=files(tmp_path)
    proc=subprocess.run([sys.executable,'-m','fin_feature.cli','review-batch','--batch-dir',str(batch),
        '--reviews',str(cfg),'--output-dir',str(tmp_path/'out')],capture_output=True,text=True)
    assert proc.returncode==1,proc.stderr
    assert json.loads(proc.stdout)['reference_match_pairs']==1
    with pytest.raises(FileExistsError):review_batch(batch,cfg,tmp_path/'out')
