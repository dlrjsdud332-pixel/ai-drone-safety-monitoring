"""Build a source-only review overlay; never init the project, delete files or push.

Remote inventory comes from a read-only pinned GitHub tree. A Git blob hash
comparison is useful before clone, but is not a clone or complete history audit.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from audit_upload import audit, ROOT


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--remote-inventory',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():p.error('Use a new output directory')
    a.output.mkdir(parents=True)
    audit(a.output/'audit')
    checked=json.loads((a.output/'audit/upload-audit.json').read_text())
    if checked['findings'] or any(not r['passed'] for r in checked['python_syntax']):
        raise SystemExit('Audit findings require review; no overlay copied')
    inventory=json.loads(a.remote_inventory.read_text())
    if inventory['repository']!='dlrjsdud332-pixel/ai-drone-safety-monitoring' or inventory['tree'].get('truncated'):
        raise ValueError('Unexpected repository or incomplete remote tree')
    remote={r['path']:r for r in inventory['tree']['tree'] if r['type']=='blob'}
    files={r['path']:r for r in checked['files']}
    diff={'added':[],'modified':[],'unchanged':[],'remote_only_or_excluded':[],'deleted':[]}
    overlay=a.output/'upload-overlay';overlay.mkdir()
    for name,row in files.items():
        source=ROOT/name
        if source.is_symlink():raise ValueError('Symlinks not allowed')
        data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError('Source changed during review')
        blob=hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()
        kind='added' if name not in remote else ('unchanged' if blob==remote[name]['sha'] else 'modified')
        diff[kind].append(name)
        dest=overlay/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    for name in sorted(set(remote)-set(files)):
        diff['remote_only_or_excluded'].append(dict(path=name,remote_bytes=remote[name]['size'],
            reason='Already tracked data/weight: final publication decision required; ignore does not untrack' if name.startswith(('data/','models/')) else 'Preserve remote file; no local overlay replacement'))
    report=dict(repository=inventory['repository'],base_commit=inventory['head'],proposed_branch='release/prototype-v0.1',
        scope='Read-only API tree vs local Git blob hash; no clone/index/stage/commit/push. Overlay is not a full clone.',diff=diff,
        files=checked['files'],bytes=checked['total_bytes'],public_ready=False,
        blockers=['Git clone unavailable: terminal DNS failure','Existing tracked data/weights require decision before publication','Project-wide distribution license remains undecided'])
    (a.output/'manifest.json').write_text(json.dumps(report,indent=2))
    (a.output/'include-files.txt').write_text('\n'.join(sorted(files))+'\n')
    (a.output/'remote-excluded-or-preserve.txt').write_text('\n'.join(r['path'] for r in diff['remote_only_or_excluded'])+'\n')
    print(json.dumps(dict(files=len(files),bytes=report['bytes'],changes={k:len(v) for k,v in diff.items()},public_ready=False),indent=2))


if __name__=='__main__':main()
