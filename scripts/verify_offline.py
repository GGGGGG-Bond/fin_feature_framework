"""One-command clean-checkout acceptance. Every runtime output uses a new directory."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify(output):
    output=Path(output).expanduser().resolve();output.mkdir(parents=True,exist_ok=False)
    commands=[]
    def run(name,args,expected=0):
        with (output/(name+'.log')).open('w') as log:
            result=subprocess.run([sys.executable,*args],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        commands.append(dict(name=name,exit_code=result.returncode,expected_exit_code=expected))
        if result.returncode!=expected:
            raise RuntimeError(f'{name} exited {result.returncode}; see {output/(name+".log")}')
    run('docs',['scripts/check_docs.py'])
    run('release-refs',['scripts/check_release_refs.py'])
    run('tests',['-m','pytest','tests/','-q','-p','no:cacheprovider','-m','not requires_network'])
    run('cli',['-m','fin_feature','--help'])
    run('showcase',['-m','examples.showcase_flow','--output-dir',str(output/'showcase')])
    run('interview',['-m','examples.interview_flow','--output-dir',str(output/'interview')])
    run('artificial',['-m','examples.offline_flow','--output-dir',str(output/'artificial')])
    run('revision',['-m','examples.revision_flow','--output-dir',str(output/'revision')])
    run('public',['-m','examples.public_flow','--output-dir',str(output/'public')])
    run('reconciliation',['-m','fin_feature','reconcile','--left','examples/reconciliation/primary.json',
                          '--right','examples/reconciliation/secondary.json'],expected=1)
    public=output/'public'
    config=output/'config.json';config.write_text(json.dumps({'database':{'path':str(public/'finance.db')}}))
    run('delivery',['-m','fin_feature','--config',str(config),'delivery-report','--output-dir',str(output/'delivery'),
                    '--review-summary','docs/stage-3/review-result-20261018.json'])
    conn=sqlite3.connect(public/'finance.db')
    try:
        run_id=conn.execute('SELECT compute_run_id FROM feature_compute_log LIMIT 1').fetchone()[0]
    finally:conn.close()
    run('replay',['-m','fin_feature','--config',str(config),'replay','--compute-run-id',run_id])
    from datetime import datetime,timezone
    when=datetime.now(timezone.utc).date().isoformat()
    run('system-asof',['-m','fin_feature','--config',str(config),'as-of','--as-of',when,'--features'])
    run('public-asof',['-m','fin_feature','--config',str(config),'as-of','--as-of',when,'--view','public'],expected=1)
    summaries={kind:json.loads((output/kind/'summary.json').read_bytes()) for kind in ('showcase','interview','artificial','revision','public')}
    report=json.loads((output/'delivery/summary.json').read_bytes())
    summary=dict(schema='offline-acceptance-v1',network_requests=0,commands=commands,demos=summaries,
                 delivery=dict(expected_features=report['expected_features'],returned_features=report['returned_features'],
                    non_null_features=report['non_null_features'],not_computable_reasons=report['not_computable_reasons'],
                    source_review=report['external_review']),
                 limitations=['This is offline acceptance, not a fresh API pull or historical environment restoration.',
                              'Public fixtures are reviewed facts in reconstructed envelopes, not original vendor responses.'])
    (output/'acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True)
    args=parser.parse_args()
    try:
        summary=verify(args.output_dir)
        print(json.dumps(summary,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,RuntimeError) as exc:
        print(f'Acceptance failed: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
