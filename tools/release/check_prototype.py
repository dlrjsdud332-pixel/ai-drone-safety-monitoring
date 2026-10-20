"""Run existing offline checks only; no training, benchmarks, GUI or flight commands."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gazebo-python',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():p.error('Use a new output directory')
    a.output.mkdir(parents=True)
    suites=[('agent',['test_agent_safety.py']),('alarm',['tools/ai/test_alarm_logic.py']),
        ('fire_dataset',['tools/ai/test_fire_dataset.py']),('fire_review',['tools/ai/test_fire_review.py']),
        ('crowd_logic',['tools/ai/test_crowd_logic.py']),('crowd_regions',['tools/ai/test_crowd_regions.py']),
        ('crowd_evaluation',['tools/ai/test_crowd_detection_evaluation.py'])]
    checks=[(name,[sys.executable,*args]) for name,args in suites]
    checks += [('gazebo',[str(a.gazebo_python),'-m','unittest','discover','-s','tools/gazebo','-p','test_*.py']),
        ('industrial',[str(a.gazebo_python),'tools/gazebo/industrial/test_industrial.py'])]
    rows=[]
    for name,cmd in checks:
        r=subprocess.run(cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
        (a.output/f'{name}.log').write_text(r.stdout)
        count=re.search(r'Ran (\d+) tests?',r.stdout)
        failure=re.search(r'failures=(\d+)',r.stdout);error=re.search(r'errors=(\d+)',r.stdout)
        row=dict(suite=name,exit_code=r.returncode,tests=int(count[1]) if count else None,
            failures=int(failure[1]) if failure else 0,errors=int(error[1]) if error else 0)
        rows.append(row);print(json.dumps(row),flush=True)
    (a.output/'summary.json').write_text(json.dumps(rows,indent=2))
    return int(any(r['exit_code'] for r in rows))


if __name__=='__main__':raise SystemExit(main())
