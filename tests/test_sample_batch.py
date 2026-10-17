import copy
import json
from types import SimpleNamespace
import pytest
from fin_feature.sample_batch import run_batch,validate_plan,verified_sample
from fin_feature.source_sample import run_sample,SINA_FIELDS,FIELDS


def plan(*codes):
    return dict(schema='sample-batch-v1',periods=['2024-12-31'],companies=[
        dict(secucode=c,name='ARTIFICIAL',group='fixture') for c in codes])


class Client:
    def __init__(self,code,missing=False):self.code=code;self.missing=missing;self.calls=0
    def get(self,url,params,timeout):
        self.calls+=1
        table='BS' if params.get('source')=='fzb' or params.get('reportName','').endswith('BALANCE') else 'IS'
        values={f:('-10' if f=='parent_netprofit' else '100') for f,(t,_) in FIELDS.items() if t==table}
        if self.missing:values['operate_cost']=None
        if 'sina' in url:
            items=[dict(item_field=SINA_FIELDS[f][0],item_title=SINA_FIELDS[f][1],item_value=v)
                   for f,v in values.items() if FIELDS[f][0]==table]
            doc={'result':{'status':{'code':0},'data':{'report_list':{'20241231':{
                'data':items,'rCurrency':'CNY','rType':'合并期末'}}}}}
        else:
            row=dict(SECUCODE=self.code,REPORT_DATE='2024-12-31')
            row.update({f.upper():v for f,v in values.items() if FIELDS[f][0]==table})
            doc=dict(success=True,result=dict(pages=1,count=1,data=[row]))
        return SimpleNamespace(status_code=200,content=json.dumps(doc).encode())


def collector(code,periods,path,*,pause):
    return run_sample(code,periods,path,session=Client(code),pause=lambda _:None)


def test_collect_two_companies_and_fixed_denominator(tmp_path):
    report=run_batch(plan('600519.SH','000333.SZ'),tmp_path/'out',collector=collector,pause=lambda _:None)
    assert report['collected_companies']==2 and report['failed_companies']==0
    assert report['planned_pairs']==12 and report['numeric_pairs']==12
    assert report['numeric_pair_coverage']==1 and report['actual_http_requests']==8
    assert report['reconciliation_counts']=={'incomparable':12}
    assert report['exit_code']==1 and report['official_reference_checked_pairs']==0
    assert (tmp_path/'out/summary.md').exists()


def test_failure_does_not_disappear_or_stop_later_company(tmp_path):
    def fail_first(code,periods,path,*,pause):
        if code=='600519.SH':return dict(status='failed',requests=[{}],error='fixture')
        return collector(code,periods,path,pause=pause)
    report=run_batch(plan('600519.SH','000333.SZ'),tmp_path/'out',collector=fail_first,pause=lambda _:None)
    assert report['collected_companies']==1 and report['failed_companies']==1
    assert report['numeric_pair_coverage']==0.5 and report['planned_pairs']==12
    assert report['reconciliation_counts']=={'collection_failed':6,'incomparable':6}
    assert report['exit_code']==2 and report['actual_http_requests']==5


def test_reuse_is_readonly_and_reconstructs_evidence(tmp_path):
    sample=tmp_path/'saved';collector('600519.SH',['2024-12-31'],sample,pause=lambda _:None)
    before={str(p.relative_to(sample)):p.read_bytes() for p in sample.rglob('*') if p.is_file()}
    p=plan('600519.SH');p['companies'][0]['reuse_sample']=str(sample)
    def forbidden(*args,**kw):raise AssertionError('network must not be used')
    report=run_batch(p,tmp_path/'out',collector=forbidden)
    assert report['actual_http_requests']==0 and report['collected_companies']==1
    assert before=={str(p.relative_to(sample)):p.read_bytes() for p in sample.rglob('*') if p.is_file()}


@pytest.mark.parametrize('corruption',['body','mapping','scope'])
def test_reuse_corruption_is_failure_without_refetch(tmp_path,corruption):
    sample=tmp_path/'saved';collector('600519.SH',['2024-12-31'],sample,pause=lambda _:None)
    if corruption=='body':next(sample.rglob('*.gz')).write_bytes(b'corrupt')
    elif corruption=='mapping':
        p=sample/'sina.json';d=json.loads(p.read_text());d['records'][0]['value']='999';p.write_text(json.dumps(d))
    else:
        p=sample/'manifest.json';d=json.loads(p.read_text());d['periods']=['2023-12-31'];p.write_text(json.dumps(d))
    p=plan('600519.SH');p['companies'][0]['reuse_sample']=str(sample)
    def forbidden(*a,**kw):raise AssertionError('no refetch on damaged reuse')
    report=run_batch(p,tmp_path/'out',collector=forbidden)
    assert report['failed_companies']==1 and report['actual_http_requests']==0
    assert report['numeric_pair_coverage']==0


@pytest.mark.parametrize('mutation',['empty','too_many','duplicate','period','missing_label'])
def test_bad_plan_rejected_before_files_or_network(tmp_path,mutation):
    p=plan('600519.SH')
    if mutation=='empty':p['companies']=[]
    elif mutation=='too_many':p['companies']*=9
    elif mutation=='duplicate':p['companies']*=2
    elif mutation=='period':p['periods']=['2024-09-30']
    else:p['companies'][0].pop('group')
    with pytest.raises(ValueError):run_batch(p,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_missing_value_and_negative_profit_coverage(tmp_path):
    def collect(code,periods,path,*,pause):
        return run_sample(code,periods,path,session=Client(code,missing=True),pause=lambda _:None)
    r=run_batch(plan('600519.SH'),tmp_path/'out',collector=collect,pause=lambda _:None)
    assert r['numeric_pairs']==5 and r['provider_numeric_values']=={'eastmoney':5,'sina':5}
    assert r['reconciliation_counts']=={'incomparable':5,'missing':1}


def test_existing_output_refused(tmp_path):
    with pytest.raises(FileExistsError):run_batch(plan('600519.SH'),tmp_path)


def test_offline_rebuild_preserves_original_failure(tmp_path):
    from fin_feature.sample_batch import rebuild_saved_sample
    sample=tmp_path/'saved';collector('600519.SH',['2024-12-31'],sample,pause=lambda _:None)
    p=sample/'manifest.json';m=json.loads(p.read_text());m.update(status='failed',error='old parser')
    m['requests'][-1]['outcome']='received';p.write_text(json.dumps(m))
    original=p.read_bytes();out=tmp_path/'rebuilt'
    result=rebuild_saved_sample(sample,out)
    assert result['original_status']=='failed' and result['status']=='collected'
    assert p.read_bytes()==original
    assert verified_sample(out,'600519.SH',['2024-12-31'])


def test_offline_rebuild_rejects_incomplete_evidence_before_output(tmp_path):
    from fin_feature.sample_batch import rebuild_saved_sample
    sample=tmp_path/'saved';collector('600519.SH',['2024-12-31'],sample,pause=lambda _:None)
    p=sample/'manifest.json';m=json.loads(p.read_text());m['requests'].pop();p.write_text(json.dumps(m))
    with pytest.raises(ValueError):rebuild_saved_sample(sample,tmp_path/'out')
    assert not (tmp_path/'out').exists()
