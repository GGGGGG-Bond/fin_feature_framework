"""Bounded capture uses real temporary evidence files with scripted HTTP only."""
import json
from pathlib import Path

import pytest
import requests
from examples.showcase_flow import load_snapshots, SnapshotSession
from fin_feature import capture_showcase as live


def response(payload,status=200):
    result=requests.Response();result.status_code=status;result.encoding='utf-8'
    result.url=live.PRICE_URL;result._content=json.dumps(payload).encode()
    return result


def test_price_failure_keeps_received_body_and_planned_denominator(tmp_path):
    class Session:
        calls=0
        def get(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:return response({'error':'received failure'},500)
            return response({'code':0,'data':{'sh510300':{'qfqday':[['2025-05-06','100','101'],['2025-12-31','101','103']]}}})
    result,attempts=live.capture_prices([dict(secucode='600519.SH',name='test')],tmp_path/'prices',session=Session(),pause=lambda _:None)
    assert len(attempts)==2 and len(result['instruments'])==1
    assert attempts[0]['status']=='failed' and attempts[1]['status']=='collected'
    import gzip,hashlib
    body=gzip.decompress((tmp_path/'prices/raw'/attempts[0]['file_path']).read_bytes())
    assert json.loads(body)['error']=='received failure' and hashlib.sha256(body).hexdigest()==attempts[0]['raw_sha256']


def test_no_unadjusted_fallback():
    with pytest.raises(ValueError,match='adjusted'):
        live.price_rows({'code':0,'data':{'sh600519':{'day':[['2025-05-06','1','2']]}}},'sh600519')


def test_malformed_price_envelopes_rejected():
    for bad in ([],dict(code=False),dict(code=0,data=[]),dict(code=0,data={'sh600519':None}),
                dict(code=0,data={'sh600519':dict(qfqday=[None])})):
        with pytest.raises(ValueError):live.price_rows(bad,'sh600519')


@pytest.mark.parametrize('rows',[
    [['2025-05-06','1','2'],['2025-05-06','1','2']],
    [['2025-05-06','0','2']], [['2025-05-06','1','NaN']], [['2026-05-06','1','2']],
])
def test_invalid_price_capture_rejected(rows):
    with pytest.raises(ValueError):live.price_rows({'code':0,'data':{'sh600519':{'qfqday':rows}}},'sh600519')


def test_capture_financial_export_joins_actual_schema_and_does_not_modify_fixtures(tmp_path,monkeypatch):
    snapshots=load_snapshots()
    def fake_batch(plan,output):
        output=Path(output);output.mkdir();companies=[]
        for c in snapshots['dual-source.json']['companies']:
            path=output/c['secucode'];path.mkdir()
            for provider,doc in c['providers'].items():live.save(path/(provider+'.json'),doc)
            live.save(path/'manifest.json',dict(requests=[{**r,'snapshot':dict(raw_hash=r['raw_sha256'])} for r in c['requests']]))
            companies.append({k:c[k] for k in ('secucode','name','group')}|dict(status='collected',sample_dir=str(path)))
        return dict(companies=companies,actual_http_requests=32)
    class Session(SnapshotSession):
        def __init__(self):super().__init__(snapshots['financials.json']['pages']);self.headers={}
        def get(self,url,*,params,timeout):
            if url!=live.PRICE_URL:return super().get(url,params=params,timeout=timeout)
            symbol=params['param'].split(',')[0];code=symbol[2:]+'.'+symbol[:2].upper()
            instrument=next(r for r in snapshots['prices.json']['instruments'] if r['secucode']==code)
            return response(dict(code=0,data={symbol:dict(qfqday=[[b['date'],b['open'],b['close']] for b in instrument['bars']])}))
        def close(self):pass
    monkeypatch.setattr(live,'run_batch',fake_batch)
    monkeypatch.setattr(live.requests,'Session',Session)
    monkeypatch.setattr(live.RateLimiter,'wait',lambda _:None)
    original=live.capture_prices
    monkeypatch.setattr(live,'capture_prices',lambda c,p:original(c,p,pause=lambda _:None))
    summary=live.capture(tmp_path/'capture')
    assert summary['status']=='success' and summary['primary_pages']==24 and summary['primary_attempts']==24
    assert summary['price_successes']==9 and summary['dual_companies']==8
    financials=json.loads((tmp_path/'capture/financials.json').read_bytes())
    assert len(financials['pages'])==24 and all('request_params' in p for p in financials['pages'])
    load_snapshots()  # Capture does not overwrite or repin Git fixtures.
