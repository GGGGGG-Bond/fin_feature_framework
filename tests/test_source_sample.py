import gzip
import hashlib
import json
from types import SimpleNamespace
import pytest
import requests
from fin_feature.source_sample import run_sample, map_sina, map_eastmoney, SINA_FIELDS, FIELDS


def sina(table, period='20241231'):
    items=[dict(item_field=code,item_title=title,item_value='100.00')
           for field,(code,title) in SINA_FIELDS.items() if FIELDS[field][0]==table]
    return {'result':{'status':{'code':0},'data':{'report_count':100,'report_list':{period:{
        'rCurrency':'CNY','rType':'合并期末','publish_date':'20260417','update_time':1743599711,'data':items}}}}}


def em(table):
    record={'SECUCODE':'600519.SH','REPORT_DATE':'2024-12-31 00:00:00','NOTICE_DATE':'2026-04-17'}
    record.update({k.upper():100 for k,(t,_) in FIELDS.items() if t==table})
    return {'success':True,'result':{'pages':1,'count':1,'data':[record]}}


class Client:
    def __init__(self, failure=None): self.calls=[];self.failure=failure
    def get(self,url,params,timeout):
        self.calls.append((url,params,timeout))
        if self.failure=='transport': raise requests.Timeout('fixture timeout')
        if self.failure=='http': return SimpleNamespace(status_code=503,content=b'not available')
        if self.failure=='json': return SimpleNamespace(status_code=200,content=b'invalid json')
        if 'sina' in url: doc=sina('BS' if params['source']=='fzb' else 'IS')
        else: doc=em('BS' if params['reportName'].endswith('BALANCE') else 'IS')
        return SimpleNamespace(status_code=200,content=json.dumps(doc).encode())


def test_full_sample_keeps_evidence_unknowns_and_dates(tmp_path):
    client=Client();out=tmp_path/'sample'
    result=run_sample('600519.SH',['2024-12-31'],out,session=client,pause=lambda _:None)
    assert result['status']=='collected' and result['exit_code']==1
    assert result['counts']=={'incomparable':6}
    assert len(client.calls)==4
    for request in result['requests']:
        snap=request['snapshot'];body=gzip.decompress((out/'raw'/snap['file_path']).read_bytes())
        assert hashlib.sha256(body).hexdigest()==snap['raw_hash']
        assert request['outcome']=='ok'
    rows=json.loads((out/'sina.json').read_text())['records']
    assert all(r['unit']=='unknown' and r['comparison_basis_id']=='unknown' for r in rows)
    assert rows[0]['provider_metadata']['publish_date']=='20260417'
    assert not list(out.rglob('*.db'))


@pytest.mark.parametrize('failure', ['transport','http','json'])
def test_failure_keeps_received_body_and_no_report(tmp_path,failure):
    client=Client(failure);out=tmp_path/'failed'
    result=run_sample('600519.SH',['2024-12-31'],out,session=client,pause=lambda _:None)
    assert result['exit_code']==2 and result['status']=='failed'
    assert len(client.calls)==1 and (out/'manifest.json').exists()
    assert not (out/'report.json').exists()
    assert len(list(out.rglob('*.gz')))==(0 if failure=='transport' else 1)


@pytest.mark.parametrize('code,periods',[('600519', ['2024-12-31']),('600519.SH',['2024-09-30']),
    ('600519.SH',[]),('600519.SH',['2024-12-31']*2)])
def test_scope_rejected_before_network_or_directory(tmp_path,code,periods):
    client=Client();out=tmp_path/'out'
    with pytest.raises(ValueError): run_sample(code,periods,out,session=client)
    assert not client.calls and not out.exists()


def test_existing_directory_refused(tmp_path):
    with pytest.raises(FileExistsError): run_sample('600519.SH',['2024-12-31'],tmp_path,session=Client())


@pytest.mark.parametrize('mutation',['title','duplicate','missing_period','business'])
def test_sina_rejects_ambiguous_mapping(mutation):
    doc=sina('BS');report=doc['result']['data']['report_list']['20241231']
    if mutation=='title': report['data'][0]['item_title']='different meaning'
    if mutation=='duplicate': report['data'].append(report['data'][0])
    if mutation=='missing_period': doc['result']['data']['report_list']={}
    if mutation=='business': doc['result']['status']['code']=True
    with pytest.raises(ValueError): map_sina(json.dumps(doc),'600519.SH',['2024-12-31'],'BS','evidence')


@pytest.mark.parametrize('mutation',['company','pages','count','duplicate'])
def test_em_rejects_bad_identity_and_incomplete_page(mutation):
    doc=em('BS');result=doc['result']
    if mutation=='company': result['data'][0]['SECUCODE']='000001.SZ'
    if mutation=='pages': result['pages']=2
    if mutation=='count': result['count']=True
    if mutation=='duplicate': result['data'].append(result['data'][0]);result['count']=2
    with pytest.raises(ValueError): map_eastmoney(json.dumps(doc),'600519.SH',['2024-12-31'],'BS','evidence')


def test_storage_failure_prevents_mapping_and_followup(tmp_path,monkeypatch):
    from fin_feature.storage.raw_store import RawStore,RawStoreError
    def fail(*args): raise RawStoreError('fixture disk failure')
    monkeypatch.setattr(RawStore,'publish',fail)
    client=Client();out=tmp_path/'out'
    result=run_sample('600519.SH',['2024-12-31'],out,session=client,pause=lambda _:None)
    assert result['exit_code']==2 and len(client.calls)==1
    assert not (out/'sina.json').exists() and not (out/'report.json').exists()


def test_no_parent_equity_substitution():
    doc=sina('BS');items=doc['result']['data']['report_list']['20241231']['data']
    items[:]=[i for i in items if i['item_field']!='RIGHAGGR']
    items.append(dict(item_field='PARESHARRIGH',item_title='归属于母公司股东权益合计',item_value='99'))
    rows=map_sina(json.dumps(doc),'600519.SH',['2024-12-31'],'BS','evidence')
    assert next(r for r in rows if r['field']=='total_equity')['value'] is None


def test_confirmed_broker_parent_profit_title_alias():
    doc=sina('IS')
    for item in doc['result']['data']['report_list']['20241231']['data']:
        if item['item_field']=='PARENETP':item['item_title']='归属于母公司的净利润'
    rows=map_sina(json.dumps(doc),'600030.SH',['2024-12-31'],'IS','evidence')
    assert next(r for r in rows if r['field']=='parent_netprofit')['value']=='100.00'
