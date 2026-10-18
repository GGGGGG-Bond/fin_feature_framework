"""Three companies with content-bound issuer facts, independent of provider claims."""
import json
from pathlib import Path

from examples.interview_flow import load_facts, run_interview_demo
from fin_feature.storage.hashing import is_valid_hash

ROOT = Path(__file__).with_name('reviewed')
IDENTITIES = {
    'catl.json': 'f0ccfc8cc99d10917b01a837b0d9abccba934c2917a960370760338e26809e52',
    'midea.json': '2e3dd03e5eb016197f1b5f84a83ba5fd3d0fe207eb44bd8e8e5c4d86b48c769c',
    'moutai.json': '37263bf7b36204280e256feb452d75ec4e3577f64dd90d343a2529051b022572',
}


def load_reviewed(root=ROOT):
    """Validate pinned numerical content before any runtime directory is created."""
    from datetime import date
    from decimal import Decimal
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_bytes())
    if manifest.get('files') != IDENTITIES:
        raise ValueError('reviewed report manifest changed; review new facts first')
    result = {}
    for filename, identity in IDENTITIES.items():
        fixture = load_facts(root / filename, identity)
        docs = {d['id']: d for d in fixture['documents']}
        if len(docs) != len(fixture['documents']):
            raise ValueError('duplicate reviewed document')
        for document in docs.values():
            if not is_valid_hash(document.get('sha256')) or not document.get('url', '').startswith('https://'):
                raise ValueError('reviewed document identity is incomplete')
            if date.fromisoformat(document['available_by']).isoformat() != document['available_by']:
                raise ValueError('normalized disclosure bound required')
        seen = set()
        for fact in fixture['facts']:
            key = fact['report_date'], fact['field']
            if key in seen or fact['document'] not in docs or not isinstance(fact.get('page'), int) or fact['page'] < 1:
                raise ValueError('duplicate fact or missing physical PDF position')
            if not Decimal(fact['value']).is_finite():
                raise ValueError('finite reviewed fact required')
            seen.add(key)
        result[filename] = fixture
    return result


def availability_for(fixture, periods):
    """Conservative all-input bound; comparative columns inherit their document.

    This never infers first/revision times or backdates a 2023 comparison column
    carried by a 2024 annual report to a hypothetical original 2023 disclosure.
    """
    docs = {d['id']: d for d in fixture['documents']}
    selected = {f['document'] for f in fixture['facts'] if f['report_date'] in periods}
    if not selected or set(periods) - {f['report_date'] for f in fixture['facts']}:
        raise ValueError('all requested periods need reviewed facts')
    references = [docs[key] for key in sorted(selected)]
    return dict(available_by=max(d['available_by'] for d in references),
                first_public_at=None, revision_public_at=None,
                historical_archive_verified=False, documents=references,
                basis='Maximum conservative document bound of all selected input periods; no exact first/revision timestamp inferred.')


def run_verified(output):
    fixtures = load_reviewed()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    companies, targets, catalog = [], [], []
    for filename, fixture in fixtures.items():
        summary = run_interview_demo(output / filename[:-5], ROOT / filename, IDENTITIES[filename])
        companies.append(dict(name=fixture['name'], secucode=fixture['secucode'], **summary))
        available = availability_for(fixture, {f['report_date'] for f in fixture['facts']})
        catalog.append(dict(secucode=fixture['secucode'], report_date=fixture['target_period'], **available))
        targets.extend(dict(secucode=fixture['secucode'], name=fixture['name'], report_date=fixture['target_period'],
                            input_basis='reviewed_issuer_facts', available_by=available['available_by'], **row)
                       for row in summary['target_features'])
    result = dict(schema='verified-companies-v1', companies=companies, target_features=targets,
                  reviewed_facts=sum(len(f['facts']) for f in fixtures.values()),
                  target_positions=len(targets), target_finite=sum(r['feature_value'] is not None for r in targets),
                  features=sum(c['features'] for c in companies), non_null_features=sum(c['non_null_features'] for c in companies),
                  replay_count=sum(c['replay_count'] for c in companies), replay_matches=sum(c['replay_matches'] for c in companies),
                  evidence_audit_clean=all(c['evidence_audit_clean'] for c in companies),
                  network_requests=0, provider_records_approved=False)
    for name, data in [('summary.json', result), ('disclosure-catalog.json', catalog)]:
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result
