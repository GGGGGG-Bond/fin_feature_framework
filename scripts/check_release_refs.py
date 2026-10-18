"""Check current handoff download references against the project version, offline."""
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = ['README.md', 'docs/project-brief.md', 'docs/project-brief.html',
                'docs/interviewer-handoff.md', 'docs/submission-message.md']
STATUS_DOCS = ['docs/progress.md', 'docs/gaps-and-weaknesses.md', 'docs/verified-coverage.md']


def check(root=ROOT):
    root = Path(root)
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    errors, references = [], 0
    for filename in CURRENT_DOCS:
        body = (root / filename).read_text(encoding='utf-8')
        urls = re.findall(r'https://github\.com/GGGGGG-Bond/fin_feature_framework/releases/(?:tag|download)/v([^\s)<>"/]+)', body)
        if not urls:
            errors.append(dict(file=filename, error='missing fixed release reference'))
        for found in urls:
            references += 1
            if found != version:
                errors.append(dict(file=filename, expected=version, found=found))
    for filename in STATUS_DOCS:
        body = (root / filename).read_text(encoding='utf-8')
        found = re.search(r'当前版本[\s*：:]*v?(\d+\.\d+\.\d+)', body)
        if found is None or found[1] != version:
            errors.append(dict(file=filename, expected=version, found=found[1] if found else None))
    return dict(schema='release-reference-check-v1', version=version, files=len(CURRENT_DOCS), references=references,
                status_files=len(STATUS_DOCS),
                errors=errors, external_http_requests=0)


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result['errors'] else 0)
