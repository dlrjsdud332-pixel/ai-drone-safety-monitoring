"""Pinned file/code-signature collector and side-effect-free Mach-O observation.

No signing, deployment, sudo, accounts or service operations. Runtime codesign
queries are a separate explicit read path; offline tests inject command fixtures.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
from .access import ancestors,validate_node
from .agent_activation import RootPublicDocument
from .spec import SecurityError


class PinnedArtifactCollector:
    def __init__(self,document,metadata,runner=subprocess.run):
        if type(document) is not RootPublicDocument or document.path!='/Library/Application Support/AIDroneSecure/public/code-closure.json':
            raise SecurityError('Root code closure authority required')
        self.document,self.metadata,self.runner=document,metadata,runner

    def _command(self,arguments):
        result=self.runner(arguments,capture_output=True,text=True,timeout=5,shell=False)
        if len(result.stdout)+len(result.stderr)>131072:raise SecurityError('Code metadata output exceeded bound')
        return result.returncode,result.stdout+result.stderr

    def __call__(self):
        policy=self.document.read()
        if set(policy)!={'version','artifacts'} or type(policy['version']) is not int or policy['version']!=1:
            raise SecurityError('Pinned artifact policy schema rejected')
        items=policy['artifacts']
        if not isinstance(items,list) or not 1<=len(items)<=256:raise SecurityError('Pinned artifact budget rejected')
        rows=[];seen=set()
        for item in items:
            if set(item)!={'path','sha256','kind','team','cdhash','dylibs'} or item['path'] in seen:raise SecurityError('Pinned artifact entry rejected')
            path=item['path'];seen.add(path)
            if not path.startswith(('/Library/Application Support/AIDroneSecure/','/usr/lib/','/System/Library/')):
                raise SecurityError('Pinned artifact outside trusted roots')
            parents=tuple(self.metadata(p) for p in ancestors(path));node=self.metadata(path)
            if node.path!=path:raise SecurityError('Code metadata path mismatch')
            for parent,expected in zip(parents,ancestors(path)):
                if parent.path!=expected or parent.uid!=0 or parent.kind!='dir' or parent.mode&0o022 or parent.acl:raise SecurityError('Untrusted code ancestor')
            if node.mode not in (0o444,0o555,0o644,0o755):raise SecurityError('Unsafe code mode')
            validate_node(node,uid=0,kind='file',mode=node.mode)
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
            try:
                before=os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_ino!=node.inode or before.st_dev!=node.device
                        or stat.S_IMODE(before.st_mode)!=node.mode or before.st_nlink!=1 or before.st_size>256*1024*1024):raise SecurityError('Code descriptor metadata changed')
                digest=hashlib.sha256()
                while True:
                    data=os.read(fd,65536)
                    if not data:break
                    digest.update(data)
                after=os.fstat(fd)
                if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise SecurityError('Code changed during hashing')
            finally:os.close(fd)
            if digest.hexdigest()!=item['sha256']:raise SecurityError('Pinned code content mismatch')
            from dataclasses import replace
            if replace(self.metadata(path),captured_at=None)!=replace(node,captured_at=None):raise SecurityError('Code ACL metadata changed')
            if item['kind']=='script':
                verified=team=cdhash=None;dylibs=()
            elif item['kind']=='macho':
                verify_rc,_=self._command(['/usr/bin/codesign','--verify','--strict',path])
                display_rc,text=self._command(['/usr/bin/codesign','-dv','--verbose=4',path])
                req_rc,requirement=self._command(['/usr/bin/codesign','-d','-r-',path])
                dylib_rc,links=self._command(['/usr/bin/otool','-L',path])
                verified=verify_rc==0 and display_rc==0 and req_rc==0 and dylib_rc==0 and 'Signature=adhoc' not in text
                selected=re.search(r'^TeamIdentifier=([^\n]+)',text,re.M)
                team=selected.group(1).strip() if selected else None
                if team=='not set':team=None
                if team is None and 'anchor apple' in requirement and 'anchor apple generic' not in requirement and verified:
                    team='APPLE_PLATFORM'
                selected=re.search(r'^CDHash=([0-9a-f]+)',text,re.M);cdhash=selected.group(1) if selected else None
                dylibs=tuple(line.strip().split(' (',1)[0] for line in links.splitlines()[1:] if line.strip())
                # otool -L includes a dylib's own LC_ID_DYLIB before loads.
                if dylibs and dylibs[0]==path:dylibs=dylibs[1:]
            else:raise SecurityError('Pinned code kind rejected')
            rows.append(dict(node=node,ancestors=parents,sha256=digest.hexdigest(),expected_sha256=item['sha256'],kind=item['kind'],
                signature_valid=verified,team=team,expected_team=item['team'],cdhash=cdhash,expected_cdhash=item['cdhash'],dylibs=dylibs,expected_dylibs=tuple(item['dylibs'])))
        return tuple(rows)


def inspect_macho_bytes(data):
    """Parse embedded metadata only. Presence of a signature is NOT validity."""
    if type(data) is not bytes or len(data)<32 or len(data)>256*1024*1024:raise SecurityError('Mach-O size rejected')
    magic,cpu,_,_,ncmds,sizeofcmds,_,_=struct.unpack_from('<IiiIIIII',data)
    if magic!=0xfeedfacf or ncmds>4096 or 32+sizeofcmds>len(data):raise SecurityError('Thin little-endian Mach-O64 required')
    cursor=32;signature=None
    for _ in range(ncmds):
        if cursor+8>32+sizeofcmds:raise SecurityError('Mach-O command bound rejected')
        command,size=struct.unpack_from('<II',data,cursor)
        if size<8 or cursor+size>32+sizeofcmds:raise SecurityError('Mach-O command size rejected')
        if command==0x1d:
            if size<16 or signature is not None:raise SecurityError('Mach-O signature command rejected')
            offset,length=struct.unpack_from('<II',data,cursor+8)
            if offset+length>len(data):raise SecurityError('Mach-O signature payload rejected')
            signature=data[offset:offset+length]
        cursor+=size
    if cursor!=32+sizeofcmds:raise SecurityError('Mach-O load command coverage rejected')
    kind='NONE';flags=[];cms=False
    if signature is not None:
        if len(signature)<12:raise SecurityError('Signature blob truncated')
        magic,total,count=struct.unpack_from('>III',signature)
        if magic!=0xfade0cc0 or total>len(signature) or count>128 or 12+count*8>total:
            raise SecurityError('Signature superblob rejected')
        for index in range(count):
            slot,offset=struct.unpack_from('>II',signature,12+index*8)
            if offset+8>total:raise SecurityError('Signature subblob rejected')
            submagic,length=struct.unpack_from('>II',signature,offset)
            if length<8 or offset+length>total:raise SecurityError('Signature subblob bounds rejected')
            if submagic==0xfade0c02:
                if length<16:raise SecurityError('CodeDirectory truncated')
                flags.append(struct.unpack_from('>I',signature,offset+12)[0])
            if slot==0x10000:cms=True
        kind='ADHOC' if any(flag&2 for flag in flags) else 'CMS_PRESENT_UNVERIFIED' if cms else 'UNVERIFIED'
    return dict(status='PASS',scope='STATIC_MACHO_METADATA_ONLY',architecture='arm64' if cpu==0x0100000c else 'OTHER',
        signature_kind=kind,code_directory_flags=flags,cms_present=cms,signature_validity='UNVERIFIED',production_activation='BLOCKED',binary_executed=False)


def inspect_independent_binary(path):
    from .verify_integrated import ROOT
    expected=ROOT/'diagnostics/secure_r1_r2/px4-build/bin/px4'
    if Path(path)!=expected or expected.is_symlink():raise SecurityError('Independent developer binary required')
    data=expected.read_bytes();result=inspect_macho_bytes(data);result['sha256']=hashlib.sha256(data).hexdigest()
    return result
