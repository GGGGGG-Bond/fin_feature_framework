"""Revalidate sample evidence and aggregate explicit official-document reviews."""
import json
from collections import Counter
from pathlib import Path
from fin_feature.reconcile import FIELDS,index_document,number
from fin_feature.sample_batch import validate_plan,verified_sample
from fin_feature.reference_check import check_reference
from fin_feature.source_sample import write_json
from fin_feature.storage.raw_store import RawStoreError


def review_batch(batch_dir,reviews_path,output_dir):
    batch=Path(batch_dir);config_path=Path(reviews_path)
    plan=json.loads((batch/'plan.json').read_bytes());companies,periods=validate_plan(plan)
    config=json.loads(config_path.read_bytes())
    if not isinstance(config,dict) or config.get('schema')!='reference-review-plan-v1' or not isinstance(config.get('reviews'),list):
        raise ValueError('reference-review-plan-v1 with reviews list required')
    reviews={};codes={c['secucode'] for c in companies}
    for item in config['reviews']:
        if not isinstance(item,dict):raise ValueError('review entry must be an object')
        code=item.get('secucode')
        if code not in codes or code in reviews:raise ValueError('unknown or duplicate reviewed company')
        if any(not isinstance(item.get(k),str) or not item[k].strip() for k in ('reference','document')):
            raise ValueError('review reference/document paths required')
        reviews[code]=item
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=False)
    states={};rows=[];errors=[];numeric=0;checked=0;matched=0
    for company in companies:
        code=company['secucode'];keys={(code,p,f) for p in periods for f in FIELDS}
        source=Path(company.get('reuse_sample') or batch/code).expanduser()
        if company.get('reuse_sample') and not source.is_absolute():source=batch/source
        row=dict(secucode=code,name=company['name'],planned_pairs=len(keys),numeric_pairs=0,reference_checked_pairs=0)
        try:
            manifest,docs,hashes=verified_sample(source,code,periods)
            maps={s:index_document(d) for s,d in docs.items()}
            for key in keys:
                valid=all(number(maps[s][key].get('value')) is not None for s in maps)
                states[key]='unreviewed' if valid else 'source_missing'
                if valid:row['numeric_pairs']+=1
            numeric+=row['numeric_pairs'];row['input_sha256']=hashes
        except (ValueError,TypeError,KeyError,AttributeError,OSError,RawStoreError) as exc:
            for key in keys:states[key]='collection_failed'
            row['error']=f'{type(exc).__name__}: {exc}';errors.append(dict(secucode=code,error=row['error']))
            rows.append(row);continue
        if code in reviews:
            item=reviews[code]
            try:
                reference=config_path.resolve().parent/item['reference']
                document=config_path.resolve().parent/item['document']
                result=check_reference(source/'eastmoney.json',source/'sina.json',reference,document)
                for entry in result['results']:
                    key=(entry['secucode'],entry['report_date'],entry['field'])
                    states[key]=entry['status']
                checked+=result['total'];row['reference_checked_pairs']=result['total']
                matches=result['counts'].get('match_reference',0);matched+=matches;row['reference_match_pairs']=matches
                row['reference_sha256']=result['reference_sha256'];row['document']=result['document']
                write_json(root/(code+'.review.json'),result)
            except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
                for key in keys:states[key]='review_failed'
                row['error']=f'{type(exc).__name__}: {exc}';errors.append(dict(secucode=code,error=row['error']))
        rows.append(row)
    counts=dict(Counter(states.values()));planned=len(companies)*len(periods)*len(FIELDS)
    assert len(states)==planned
    summary=dict(schema='reference-review-summary-v1',planned_companies=len(companies),planned_pairs=planned,
                 numeric_pairs=numeric,numeric_pair_coverage=numeric/planned,
                 reference_checked_pairs=checked,reference_match_pairs=matched,
                 unreviewed_planned_pairs=planned-checked,reference_coverage=checked/planned,
                 counts=counts,companies=rows,errors=errors,
                 historical_visibility_verified=False,provider_revision_alignment_verified=False,
                 upstream_independence_verified=False,business_db_written=False,
                 scope='numeric checks against manually reviewed official documents')
    summary['exit_code']=2 if errors else (0 if matched==planned else 1)
    write_json(root/'summary.json',summary)
    field_rows=[dict(secucode=c,report_date=p,field=f,status=status) for (c,p,f),status in sorted(states.items())]
    write_json(root/'fields.json',field_rows)
    lines=['# 官方参考核查汇总','',f'计划字段对 {planned}；双方有效数字 {numeric}；官方参考检查 {checked}；一致 {matched}。',
           f'仍未检查的计划项 {planned-checked}（含缺失与错误项），不能计为通过。','',
           '| 证券 | 双方有效数字项 | 官方参考已查项 | 参考一致项 |','|---|---:|---:|---:|']
    lines += [f"| {r['secucode']} | {r['numeric_pairs']} | {r['reference_checked_pairs']} | {r.get('reference_match_pairs',0)} |" for r in rows]
    lines+=['','仅表示固定字段的数值核查，历史可见性、来源修订身份和底层供应商独立性尚未验证。']
    (root/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


def cmd_review_batch(args):
    import sys
    try:
        result=review_batch(args.batch_dir,args.reviews,args.output_dir)
        print(json.dumps(result,ensure_ascii=False));return result['exit_code']
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print(f'Review batch failed: {exc}',file=sys.stderr);return 2
