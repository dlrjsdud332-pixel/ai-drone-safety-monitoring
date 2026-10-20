"""List ignore-filtered upload candidates and redacted findings without staging/push.

Uses an isolated temporary Git repository when the workspace has no .git.
This cannot describe additions/deletions against an unavailable remote checkout.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
PATTERNS={
 'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 'github_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
 'aws_access_id':re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
 'openai_style_key':re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b'),
 'assigned_credential':re.compile(r'''(?i)(?:api[_-]?key|password|access[_-]?token|client[_-]?secret)\s*[:=]\s*["'][^"'\s]{12,}["']'''),
 'personal_absolute_path':re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
 'email_candidate':re.compile(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}


def audit(output):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(['git','init','-q',temp],check=True)
        cmd=['git','-C',temp,'--work-tree',str(ROOT),'ls-files','--others','--exclude-standard','-z']
        names=[n for n in subprocess.check_output(cmd).decode().split('\0') if n]
    findings=[];syntax=[];files=[]
    for name in sorted(names):
        path=ROOT/name
        if not path.is_file():continue
        size=path.stat().st_size
        files.append(dict(path=name,bytes=size,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        if path.is_symlink():findings.append(dict(path=name,kind='symlink_requires_review'))
        if size>5*1024*1024:findings.append(dict(path=name,kind='large_file',bytes=size))
        if path.suffix.lower() in ['.pt','.mp4','.mov','.glb','.jpg','.png','.ppm','.webarchive']:
            findings.append(dict(path=name,kind='binary_or_capture_requires_review'))
        try:text=path.read_text()
        except (UnicodeError,OSError):continue
        for line_number,line in enumerate(text.splitlines(),1):
            for kind,pattern in PATTERNS.items():
                if pattern.search(line):findings.append(dict(path=name,line=line_number,kind=kind))
        if path.suffix=='.py':
            try:ast.parse(text,filename=name);syntax.append(dict(path=name,passed=True))
            except SyntaxError as exc:syntax.append(dict(path=name,passed=False,line=exc.lineno))
    payload=dict(local_git_present=(ROOT/'.git').exists(),
        scope='Ignored/untracked candidate snapshot, not remote Git diff; patterns are heuristic and not a privacy certification.',
        files=files,findings=findings,python_syntax=syntax,total_bytes=sum(x['bytes'] for x in files))
    (output/'upload-audit.json').write_text(json.dumps(payload,indent=2))
    (output/'include-files.txt').write_text('\n'.join(x['path'] for x in files)+'\n')
    print(json.dumps(dict(files=len(files),bytes=payload['total_bytes'],findings=len(findings),
        python_files=len(syntax),syntax_failures=sum(not x['passed'] for x in syntax)),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    audit(parser.parse_args().output.resolve())
