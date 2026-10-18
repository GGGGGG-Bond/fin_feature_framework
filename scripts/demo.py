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
    parser.add_argument('--only',choices=['all','public','revision','artificial'],default='all')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Linux required: use WSL or Codespaces; native Windows directory fsync is unsupported.')
    root=Path(__file__).resolve().parents[1]
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    output=(args.output_dir or root/'data/demo'/stamp).expanduser().resolve()
    output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    steps=[]
    for name,module in [('public','examples.public_flow'),('revision','examples.revision_flow'),('artificial','examples.offline_flow')]:
        if args.only not in ('all',name):continue
        with (output/(name+'.log')).open('wb') as log:
            result=subprocess.run([sys.executable,'-m',module,'--output-dir',str(output/name)],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
        item=dict(name=name,exit_code=result.returncode)
        if (output/name/'summary.json').exists():
            item['summary']=json.loads((output/name/'summary.json').read_text())
        steps.append(item)
        print(json.dumps(item,ensure_ascii=False),flush=True)
        if result.returncode:break
    status=dict(output_dir=str(output),financial_network_requests=0,success=bool(steps) and all(s['exit_code']==0 for s in steps),steps=steps)
    (output/'demo.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
    print('Output: '+str(output),flush=True)
    return 0 if status['success'] else 1

if __name__=='__main__':raise SystemExit(main())
