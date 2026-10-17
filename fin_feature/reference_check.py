"""Check pinned saved values against a manually reviewed official document.

This establishes numeric agreement for supplied fields, not revision/as-of alignment.
"""
import hashlib
import json
from collections import Counter
from decimal import Decimal, localcontext
from pathlib import Path
from fin_feature.reconcile import index_document, number, UNITS, FIELDS, known


DOCUMENT_UNITS = {'yuan': Decimal(1), 'wan_yuan': Decimal(10000), 'million_yuan': Decimal(1000000), 'thousand_yuan': Decimal(1000)}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def check_reference(left_path, right_path, reference_path, document_path):
    payloads = [Path(p).read_bytes() for p in (left_path,right_path,reference_path)]
    left,right,reference = [json.loads(p,parse_float=str) for p in payloads]
    li,ri = index_document(left),index_document(right)
    if left['source']==right['source'] or left['upstream'].strip().lower()==right['upstream'].strip().lower():
        raise ValueError('distinct provider sources required')
    if not isinstance(reference,dict) or reference.get('schema')!='reviewed-reference-v1':
        raise ValueError('reviewed-reference-v1 required')
    hashes = [digest(p) for p in payloads[:2]]
    if reference.get('input_sha256')!=hashes:
        raise ValueError('saved input hashes differ from reviewed scope')
    document = reference.get('document',{})
    raw_document = Path(document_path).read_bytes()
    if not raw_document.startswith(b'%PDF') or document.get('sha256')!=digest(raw_document):
        raise ValueError('official document hash mismatch or not PDF')
    if not known(document.get('url')) or not document['url'].startswith('https://') or not known(reference.get('reviewed_by')):
        raise ValueError('document URL and manual reviewer required')
    if (reference.get('currency')!='CNY' or reference.get('unit')!='yuan'
            or reference.get('scope')!='consolidated'):
        raise ValueError('reference must explicitly declare CNY/yuan/consolidated')
    # Reuse strict identity/duplicate validation; comparison does not change source semantics.
    refs = index_document(dict(schema='reconcile-v1',source='official-reference',upstream='issuer',
                               records=reference.get('records')))
    coverage = reference.get('coverage', 'exact')
    if coverage not in ('exact', 'selected-fields'):
        raise ValueError('unsupported reference coverage mode')
    if coverage == 'exact' and (refs.keys()!=li.keys() or refs.keys()!=ri.keys()):
        raise ValueError('reference and both inputs must cover the same exact identities')
    if not refs.keys() <= li.keys() or not refs.keys() <= ri.keys():
        raise ValueError('selected reference identities must exist in both inputs')
    sources = reference.get('source_units',{})
    if set(sources)!=set((left['source'],right['source'])) or any(v not in UNITS for v in sources.values()):
        raise ValueError('explicit reviewed unit per provider required')
    tolerance = number(reference.get('absolute_tolerance'))
    if tolerance is None or not Decimal(0)<=tolerance<=Decimal('0.01'):
        raise ValueError('official comparison tolerance must be between 0 and 0.01 yuan')
    results=[]
    for key in sorted(refs):
        ref=refs[key];expected=number(ref.get('value'))
        page=ref.get('page')
        if (type(page) is not int or page<1 or not known(ref.get('row_label'))
                or not known(ref.get('column_label')) or ref.get('table_type')!=FIELDS[key[2]][0]
                or ref.get('period_basis')!=FIELDS[key[2]][1] or expected is None):
            raise ValueError('reference needs finite amount, positive page, row, column and correct table/basis')
        if 'document_value' in ref or 'document_unit' in ref:
            printed = number(ref.get('document_value'))
            multiplier = DOCUMENT_UNITS.get(ref.get('document_unit'))
            if printed is None or multiplier is None:
                raise ValueError('invalid printed document amount/unit')
            with localcontext() as ctx:
                ctx.prec = 60
                if printed * multiplier != expected:
                    raise ValueError('printed amount conversion differs from reference value')
        row=dict(secucode=key[0],report_date=key[1],field=key[2],page=page,
                 row_label=ref['row_label'],column_label=ref['column_label'],reference_value=str(expected),
                 document_value=ref.get('document_value'),document_unit=ref.get('document_unit'),
                 status='match_reference',providers={})
        for doc,items in ((left,li),(right,ri)):
            original=items[key];value=number(original.get('value'));source=doc['source']
            if (original.get('table_type')!=ref['table_type'] or original.get('period_basis')!=ref['period_basis']
                    or original.get('currency') not in ('CNY','unknown')
                    or original.get('scope') not in ('consolidated','unknown')
                    or original.get('unit') not in (sources[source],'unknown')):
                raise ValueError('provider metadata conflicts with reviewed reference')
            provider=dict(original=original,reviewed_unit=sources[source])
            if value is None:
                provider.update(status='missing',reason='missing_or_invalid_value')
            else:
                with localcontext() as ctx:
                    ctx.prec=60
                    normalized=value*UNITS[sources[source]]
                    delta=abs(normalized-expected)
                    provider.update(status='match_reference' if delta<=tolerance else 'difference_reference',
                                    normalized_value=str(normalized),absolute_difference=str(delta))
            row['providers'][source]=provider
        statuses={p['status'] for p in row['providers'].values()}
        if 'missing' in statuses: row['status']='missing'
        elif 'difference_reference' in statuses: row['status']='difference_reference'
        results.append(row)
    return dict(schema='official-reference-check-v1',scope='pinned-fields-versus-reviewed-document',
                input_sha256=hashes,reference_sha256=digest(payloads[2]),document=document,
                document_hash_verified=True,document_content_check='manual review declared in reference file',
                reviewed_by=reference['reviewed_by'],absolute_tolerance=str(tolerance),
                coverage=coverage,input_pairs=len(li.keys() | ri.keys()),
                unchecked_pairs=len((li.keys() | ri.keys()) - refs.keys()),
                historical_visibility_verified=False,provider_revision_alignment_verified=False,
                total=len(results),counts=dict(Counter(r['status'] for r in results)),results=results)


def cmd_reference_check(args):
    import sys
    try:
        report=check_reference(args.left,args.right,args.reference,args.document)
        print(json.dumps(report,ensure_ascii=False,allow_nan=False))
        return 0 if report['counts'].get('match_reference',0)==report['total'] else 1
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print(f'Reference check failed: {exc}',file=sys.stderr)
        return 2
