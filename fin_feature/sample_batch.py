"""Small fixed-universe sampling with explicit coverage and verified reuse."""
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from fin_feature.reconcile import FIELDS, index_document, number, compare_documents
from fin_feature.source_sample import run_sample,validate_scope,map_sina,map_eastmoney,write_json
from fin_feature.storage.raw_store import RawStore,RawStoreError


def validate_plan(plan):
    if not isinstance(plan,dict) or plan.get('schema')!='sample-batch-v1':
        raise ValueError('sample-batch-v1 plan required')
    periods=plan.get('periods');companies=plan.get('companies')
    if not isinstance(periods,list) or not isinstance(companies,list) or not 1<=len(companies)<=8:
        raise ValueError('plan requires 1-8 companies and 1-2 annual dates')
    codes=set()
    for company in companies:
        if not isinstance(company,dict):raise ValueError('company must be an object')
        code=company.get('secucode');validate_scope(code,periods)
        if code in codes:raise ValueError('duplicate company')
        codes.add(code)
        if any(not isinstance(company.get(k),str) or not company[k].strip() for k in ('name','group')):
            raise ValueError('company name and sample group required')
        if company.get('reuse_sample') is not None and not isinstance(company['reuse_sample'],str):
            raise ValueError('reuse_sample must be a path')
    return companies,periods


def verified_sample(path,code,periods):
    root=Path(path)
    manifest=json.loads((root/'manifest.json').read_bytes())
    if (manifest.get('status')!='collected' or manifest.get('secucode')!=code
            or manifest.get('periods')!=periods):
        raise ValueError('saved sample status/company/period scope mismatch')
    requests=manifest.get('requests')
    if not isinstance(requests,list) or len(requests)!=4:
        raise ValueError('saved sample must contain exactly four successful requests')
    store=RawStore(root/'raw',readonly=True)
    rebuilt={'eastmoney':[],'sina':[]};seen=set()
    for entry in requests:
        source,table=entry['source'],entry['table'];key=(source,table)
        if key in seen or source not in rebuilt or table not in ('BS','IS'):
            raise ValueError('ambiguous source/table evidence')
        seen.add(key)
        if entry.get('outcome')!='ok' or type(entry.get('http_status')) is not int or not 200<=entry['http_status']<300:
            raise ValueError('sample contains failed request')
        snapshot=entry['snapshot']
        if not snapshot.get('raw_hash') or type(snapshot.get('raw_bytes')) is not int:
            raise ValueError('sample hash/size metadata missing')
        body=store.verify(snapshot).body
        mapper=map_sina if source=='sina' else map_eastmoney
        rebuilt[source].extend(mapper(body,code,periods,table,'raw/'+snapshot['file_path']))
    documents={};hashes={}
    expected={(code,p,f) for p in periods for f in FIELDS}
    for source in rebuilt:
        payload=(root/(source+'.json')).read_bytes();doc=json.loads(payload,parse_float=str)
        if doc.get('source')!=source or doc.get('upstream')!=source:
            raise ValueError('saved sample provider identity mismatch')
        if set(index_document(doc))!=expected or doc['records']!=rebuilt[source]:
            raise ValueError('saved mapped rows differ from evidence reconstruction')
        documents[source]=doc;hashes[source]=hashlib.sha256(payload).hexdigest()
    return manifest,documents,hashes


def run_batch(plan,output_dir,*,collector=run_sample,pause=time.sleep):
    companies,periods=validate_plan(plan)
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'plan.json',plan)
    summary=dict(schema='sample-batch-report-v1',planned_companies=len(companies),
                 planned_pairs=len(companies)*len(periods)*len(FIELDS),companies=[],
                 collected_companies=0,failed_companies=0,actual_http_requests=0,
                 provider_numeric_values={'eastmoney':0,'sina':0},reconciliation_counts={},
                 numeric_pair_coverage=0,official_reference_checked_pairs=0,
                 historical_visibility_verified=False,business_db_written=False)
    counts=Counter();numeric_pairs=0
    for index,company in enumerate(companies):
        code=company['secucode'];reuse=company.get('reuse_sample')
        path=Path(reuse).expanduser().resolve() if reuse else root/code
        row=dict(company,sample_dir=str(path.resolve()),reused=bool(reuse),status='failed',
                 http_requests=0,planned_pairs=len(periods)*len(FIELDS))
        try:
            if not reuse:
                if index:pause(1)
                result=collector(code,periods,path,pause=pause)
                row['http_requests']=len(result.get('requests',[]))
                summary['actual_http_requests']+=row['http_requests']
                if result.get('status')!='collected':
                    raise ValueError(result.get('error','sample collection failed'))
            manifest,docs,hashes=verified_sample(path,code,periods)
            report=compare_documents(docs['eastmoney'],docs['sina'])
            maps={s:index_document(doc) for s,doc in docs.items()}
            valid={s:{k for k,v in items.items() if number(v.get('value')) is not None} for s,items in maps.items()}
            pair_count=len(valid['eastmoney'] & valid['sina']);numeric_pairs+=pair_count
            for source,keys in valid.items():summary['provider_numeric_values'][source]+=len(keys)
            counts.update(report['counts'])
            row.update(status='collected',input_sha256=hashes,numeric_pairs=pair_count,
                       numeric_values={s:len(v) for s,v in valid.items()},counts=report['counts'],
                       manifest_sha256=hashlib.sha256((path/'manifest.json').read_bytes()).hexdigest())
            summary['collected_companies']+=1
        except (ValueError,TypeError,KeyError,AttributeError,OSError,RawStoreError) as exc:
            row['error']=f'{type(exc).__name__}: {exc}'
            summary['failed_companies']+=1
            counts['collection_failed']+=row['planned_pairs']
        summary['companies'].append(row)
    summary.update(reconciliation_counts=dict(counts),numeric_pairs=numeric_pairs,
                   numeric_pair_coverage=numeric_pairs/summary['planned_pairs'])
    summary['exit_code']=2 if summary['failed_companies'] else (0 if counts.get('match',0)==summary['planned_pairs'] else 1)
    write_json(root/'summary.json',summary)
    lines=['# 样本采集汇总','',f"计划 {summary['planned_companies']} 家 / {summary['planned_pairs']} 项；完成 {summary['collected_companies']} 家；失败 {summary['failed_companies']} 家。",
           f"双方有效数字覆盖 {numeric_pairs}/{summary['planned_pairs']}；本批未执行官方年报参考核查。",'',
           '| 证券 | 样本组 | 采集状态 | 双方有效数字项 | 请求数 |','|---|---|---|---:|---:|']
    for row in summary['companies']:
        group=row['group'].replace('|','/').replace('\n',' ')
        lines.append(f"| {row['secucode']} | {group} | {row['status']} | {row.get('numeric_pairs',0)} | {row['http_requests']} |")
    lines+=['','有效数字覆盖不等于口径可比或核验通过；失败项目保留在计划分母中。详细原因见 summary.json 和逐公司报告。']
    (root/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


def cmd_sample_batch(args):
    import sys
    try:
        plan=json.loads(Path(args.plan).read_bytes())
        if not isinstance(plan,dict):raise ValueError('plan must be an object')
        for c in plan.get('companies',[]):
            if isinstance(c,dict) and c.get('reuse_sample'):
                c['reuse_sample']=str((Path(args.plan).resolve().parent/c['reuse_sample']).resolve())
        report=run_batch(plan,args.output_dir)
        print(json.dumps(report,ensure_ascii=False))
        return report['exit_code']
    except (ValueError,TypeError,OSError) as exc:
        print(f'Sample batch failed: {exc}',file=sys.stderr)
        return 2


def rebuild_saved_sample(source_dir,output_dir):
    """Explicit offline reparse into a new directory; original attempt facts retained."""
    import copy
    import shutil
    from datetime import datetime,timezone
    source=Path(source_dir)
    manifest=json.loads((source/'manifest.json').read_bytes())
    code=manifest['secucode'];periods=manifest['periods'];validate_scope(code,periods)
    entries=manifest['requests']
    if len(entries)!=4 or {(e['source'],e['table']) for e in entries}!={(s,t) for s in ('eastmoney','sina') for t in ('BS','IS')}:
        raise ValueError('offline rebuild requires all four response identities')
    store=RawStore(source/'raw',readonly=True)
    documents={s:dict(schema='reconcile-v1',source=s,upstream=s,records=[]) for s in ('eastmoney','sina')}
    for entry in entries:
        if type(entry.get('http_status')) is not int or not 200<=entry['http_status']<300:
            raise ValueError('offline rebuild cannot turn HTTP failure into success')
        snapshot=entry['snapshot']
        if not snapshot.get('raw_hash') or type(snapshot.get('raw_bytes')) is not int:
            raise ValueError('offline rebuild requires body hash and size')
        body=store.verify(snapshot).body
        mapper=map_sina if entry['source']=='sina' else map_eastmoney
        documents[entry['source']]['records'].extend(mapper(body,code,periods,entry['table'],'raw/'+snapshot['file_path']))
    target=Path(output_dir);target.mkdir(parents=True,exist_ok=False)
    shutil.copytree(source/'raw',target/'raw')
    rebuilt=copy.deepcopy(manifest)
    rebuilt.update(status='collected',exit_code=1,rebuilt_from=str(source.resolve()),
                   original_manifest_sha256=hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
                   original_status=manifest.get('status'),reparsed_at=datetime.now(timezone.utc).isoformat())
    rebuilt.pop('error',None)
    for entry in rebuilt['requests']:entry['outcome']='ok'
    for name,doc in documents.items():write_json(target/(name+'.json'),doc)
    report=compare_documents(documents['eastmoney'],documents['sina'])
    rebuilt.update(counts=report['counts'],total=report['total'])
    write_json(target/'report.json',report);write_json(target/'manifest.json',rebuilt)
    verified_sample(target,code,periods)
    return rebuilt
