"""Run offline examples without requesting a financial data source."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',choices=['all','interview','public','revision','artificial'],default='interview')
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--open',action='store_true',help='Ask the local browser to open the HTML overview')
    args=parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Linux required: use WSL or Codespaces; native Windows directory fsync is unsupported.')
    root=Path(__file__).resolve().parents[1]
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    output=(args.output_dir or root/'data/demo'/stamp).expanduser().resolve()
    output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    steps=[]
    for name,module in [('interview','examples.interview_flow'),('public','examples.public_flow'),('revision','examples.revision_flow'),('artificial','examples.offline_flow')]:
        if args.only not in ('all',name):continue
        with (output/(name+'.log')).open('wb') as log:
            result=subprocess.run([sys.executable,'-m',module,'--output-dir',str(output/name)],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
        item=dict(name=name,exit_code=result.returncode)
        if (output/name/'summary.json').exists():
            item['summary']=json.loads((output/name/'summary.json').read_text())
        steps.append(item)
        print(f'{name}: exit={result.returncode}; replay={item.get("summary",{}).get("replay_matches",0)}/'
              f'{item.get("summary",{}).get("replay_count",0)}',flush=True)
        if result.returncode:break
    status=dict(output_dir=str(output),financial_network_requests=0,success=bool(steps) and all(s['exit_code']==0 for s in steps),steps=steps)
    (output/'demo.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
    from fin_feature.demo_report import write_overview
    overview=write_overview(output,status)
    print('Overview: '+str(overview),flush=True)
    print('Output: '+str(output),flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(overview.as_uri())
    return 0 if status['success'] else 1

if __name__=='__main__':raise SystemExit(main())
