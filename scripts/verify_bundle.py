"""Verify the immutable files listed by a delivery manifest, using the stdlib."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify_bundle(root,strict=False):
    root=Path(root).resolve()
    doc=json.loads((root/'MANIFEST.json').read_bytes())
    if doc.get('schema')!='fin-delivery-manifest-v1' or not isinstance(doc.get('files'),dict) or not doc['files']:
        raise ValueError('nonempty fin-delivery-manifest-v1 required')
    for name,record in doc['files'].items():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            raise ValueError('unsafe manifest path')
        target=root
        for part in path.parts:
            target=target/part
            if target.is_symlink():raise ValueError('symlink in manifest path')
        if not target.is_file() or not target.resolve().is_relative_to(root):raise ValueError('missing file: '+name)
        body=target.read_bytes()
        if len(body)!=record['bytes'] or hashlib.sha256(body).hexdigest()!=record['sha256']:
            raise ValueError('changed file: '+name)
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    extra=sorted(actual-set(doc['files'])-{'MANIFEST.json'})
    if strict and extra:raise ValueError('unlisted files: '+str(extra))
    return dict(version=doc['version'],source_commit=doc['source_commit'],verified_files=len(doc['files']),extra_files=extra)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root');parser.add_argument('--strict',action='store_true')
    args=parser.parse_args()
    try:
        result=verify_bundle(args.root,args.strict);print(json.dumps(result,ensure_ascii=False));return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print('Verification failed: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
