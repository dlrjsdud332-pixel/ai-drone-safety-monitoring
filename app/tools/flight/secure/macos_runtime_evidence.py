"""Current Darwin runtime evidence, never saved reports or caller UID claims.

Native code reads SELF (or emits a nonce-bound snapshot inside owned PX4).
Cache members are checked explicitly under kernel enforced library validation,
SIP, exact OS/cache/image/content pins; they never masquerade as ordinary files.
No account, signing, socket, key or service mutation is performed here.
"""
import ctypes as C
import hashlib
import os
from pathlib import Path
import plistlib
import re
import struct
import sys
import time
import xml.parsers.expat  # preload native parser before image enumeration
from .spec import SecurityError,finite,integer,canonical,private_path
from .agent_activation import RootPublicDocument
from .identity_checks import RuntimeIdentity,read_kernel_identity,validate_code_closure,validate_runtime_context,read_runtime_context
from .deployment_audit import PinnedArtifactCollector
from .runtime_contract import CODE_ROOT

# Apple xnu cs_blobs.h: no write operations or code-sign status setters.
REQUIRED_FLAGS=0x1|0x100|0x200|0x2000|0x10000|0x02000000
FORBIDDEN_FLAGS=0x2|0x4|0x20|0x01000000|0x10000000
PROBE=CODE_ROOT+'/runtime/lib/runtime_probe.dylib'
DENIED_ENTITLEMENTS={'com.apple.security.get-task-allow','get-task-allow','com.apple.security.cs.allow-jit','com.apple.security.cs.allow-unsigned-executable-memory','com.apple.security.cs.disable-library-validation','com.apple.security.cs.allow-dyld-environment-variables'}


def kernel_code(pid):
    """Only CS_OPS_STATUS/CDHASH. Caller restricts to self/registered child."""
    if sys.platform!='darwin':raise SecurityError('Darwin signature evidence unavailable')
    fn=C.CDLL(None,use_errno=True).csops;fn.argtypes=[C.c_int,C.c_uint,C.c_void_p,C.c_size_t];fn.restype=C.c_int
    flags=C.c_uint32();digest=(C.c_ubyte*20)()
    if fn(pid,0,C.byref(flags),C.sizeof(flags)) or fn(pid,5,digest,20):raise SecurityError('Kernel code signature unavailable')
    return dict(cs_flags=flags.value,cdhash=bytes(digest).hex())


def _pin(value,length):
    return type(value) is str and re.fullmatch('[0-9a-f]{'+str(length)+'}',value) is not None


def validate_native(raw,policy,identity,now,*,current_kernel,contains):
    required={'version','pid','uid','euid','gid','egid','groups','host_us','cs_flags','cdhash','csr_config','os_build','cache_uuid','images'}
    if type(raw) is not dict or set(raw)!=required or type(raw['version']) is not int or raw['version']!=1:raise SecurityError('Native runtime schema rejected')
    for name in ('pid','uid','euid','gid','egid','host_us','cs_flags','csr_config'):integer(raw[name],0,2**63-1)
    if (raw['pid'],raw['uid'],raw['euid'],raw['gid'],raw['egid'])!=(identity.pid,identity.uid,identity.uid,identity.uid,identity.uid):raise SecurityError('Runtime role credentials mismatch')
    if type(raw['groups']) is not list or len(raw['groups'])>128 or any(type(g) is not int for g in raw['groups']) or sorted(set(raw['groups'])|{raw['gid']})!=policy['effective_groups']:raise SecurityError('Effective groups mismatch')
    flags=raw['cs_flags']
    if flags&REQUIRED_FLAGS!=REQUIRED_FLAGS or flags&FORBIDDEN_FLAGS or raw['csr_config']!=0:raise SecurityError('Kernel enforcement/SIP prerequisites unavailable')
    if current_kernel!={k:raw[k] for k in ('cs_flags','cdhash')} or not _pin(raw['cdhash'],40):raise SecurityError('Current kernel code identity changed')
    if not 0<=finite(now)-raw['host_us']/1e6<1:raise SecurityError('Runtime evidence stale')
    if raw['os_build']!=policy['os_build'] or raw['cache_uuid']!=policy['cache_uuid'] or not _pin(raw['cache_uuid'],32):raise SecurityError('OS/shared-cache identity mismatch')
    catalog=policy['cache_images']
    if type(catalog) is not list or not 1<=len(catalog)<=512:raise SecurityError('Explicit cache inventory required')
    pins={}
    normal_pins={}
    if type(policy['file_images']) is not list or not 1<=len(policy['file_images'])<=256:raise SecurityError('Normal image memory pins required')
    for row in policy['file_images']:
        if type(row) is not dict or set(row)!={'path','uuid','text_sha256','dylibs'} or row['path'] in normal_pins or not row['path'].startswith((CODE_ROOT+'/', '/usr/lib/','/System/Library/')) or not _pin(row['uuid'],32) or not _pin(row['text_sha256'],64) or type(row['dylibs']) is not list:raise SecurityError('Normal image pin rejected')
        private_path(row['path']);normal_pins[row['path']]=row
    for row in catalog:
        if type(row) is not dict or set(row)!={'path','uuid','text_sha256','dylibs'} or row['path'] in pins or not row['path'].startswith(('/usr/lib/','/System/Library/')) or not _pin(row['uuid'],32) or not _pin(row['text_sha256'],64) or type(row['dylibs']) is not list:raise SecurityError('Cache pin rejected')
        private_path(row['path'])
        if not contains(row['path']):raise SecurityError('Pinned cache member absent from active OS cache')
        pins[row['path']]=row
    for row in (*pins.values(),*normal_pins.values()):
        deps=row['dylibs']
        if len(deps)>256 or any(type(d) is not str for d in deps) or len(set(deps))!=len(deps):raise SecurityError('Bounded image dependencies required')
        for dep in deps:
            private_path(dep)
            if dep not in pins and dep not in normal_pins:raise SecurityError('Incomplete image dependency closure')
    images=raw['images']
    if type(images) is not list or not 1<=len(images)<=256:raise SecurityError('Bounded loaded-image inventory required')
    seen=set();cached=set();files=set()
    for row in images:
        if type(row) is not dict or set(row)!={'path','cache','uuid','text_sha256','dylibs'} or type(row['cache']) is not bool or row['path'] in seen or not _pin(row['uuid'],32) or not _pin(row['text_sha256'],64) or type(row['dylibs']) is not list:raise SecurityError('Loaded image rejected')
        path=private_path(row['path']);seen.add(path)
        if not path.startswith((CODE_ROOT+'/', '/usr/lib/','/System/Library/')):raise SecurityError('Injected loaded image')
        if row['cache']:
            expected=pins.get(path)
            if expected is None or not contains(path) or any(row[k]!=expected[k] for k in ('uuid','text_sha256','dylibs')):raise SecurityError('Loaded cache content/UUID/dependency mismatch')
            cached.add(path)
        else:
            expected=normal_pins.get(path)
            if expected is None or any(row[k]!=expected[k] for k in ('uuid','text_sha256','dylibs')):raise SecurityError('Loaded file memory/file identity mismatch')
            files.add(path)
    if identity.executable not in files:raise SecurityError('Actual main image absent')
    # Dependencies that are not loaded are catalogued cache membership, not
    # claimed current mappings. Every actually loaded image was checked above.
    return dict(cache_paths=frozenset(pins),loaded_cache=frozenset(cached),loaded_files=frozenset(files))


class MacRuntimeEvidence:
    """No caller-supplied native backend or mode. Tests patch OS methods only."""
    def __init__(self,spec,role,metadata,clock=time.monotonic):
        if role not in ('broker','px4'):raise SecurityError('Service runtime role required')
        self.spec,self.role,self.metadata,self.clock=spec,role,metadata,clock
        self.artifacts=PinnedArtifactCollector(RootPublicDocument(CODE_ROOT+'/public/code-closure.json',metadata,131072),metadata)
        self.document=RootPublicDocument(CODE_ROOT+'/public/'+spec.session_uuid+'-'+role+'-runtime.json',metadata,131072)
        self.child_document=RootPublicDocument(CODE_ROOT+'/public/'+spec.session_uuid+'-child-runtime.json',metadata,131072)
        self._documents={};self._records=None;self._stats=None;self._library=None;self.closed=False;self.last_evidence=None;self._entitlements_checked=set()
    def _policy(self,document,role):
        p=document.read();fields={'version','mode','session_uuid','epoch','spec_sha256','role','executable','effective_groups','os_build','cache_uuid','cache_images','file_images','issued_at','expires_at'}
        if set(p)!=fields or type(p['version']) is not int or p['version']!=1 or (p['mode'],p['session_uuid'],p['epoch'],p['spec_sha256'],p['role'])!=('live',self.spec.session_uuid,self.spec.epoch,self.spec.digest,role):raise SecurityError('Runtime pin authority mismatch')
        uid=self.spec.broker_uid if role=='broker' else self.spec.px4_uid
        if p['effective_groups']!=[uid] or type(p['effective_groups'][0]) is not int:raise SecurityError('Minimal independent group policy required')
        start,end=finite(p['issued_at']),finite(p['expires_at']);now=self.clock()
        if not self.spec.issued_at<=start<=now<end<=self.spec.expires_at or not 0<end-start<=300:raise SecurityError('Runtime policy expired')
        digest=hashlib.sha256(canonical(p)).hexdigest();key=document.path
        if key in self._documents and self._documents[key]!=digest:raise SecurityError('Runtime policy changed within session')
        self._documents[key]=digest;return p
    def _stable(self,records):
        result=[]
        for r in records:
            for n in (*r['ancestors'],r['node']):
                cur=self.metadata(n.path)
                if (cur.uid,cur.mode,cur.kind,cur.inode,cur.device,cur.nlink,cur.acl,cur.size)!=(n.uid,n.mode,n.kind,n.inode,n.device,n.nlink,n.acl,n.size):raise SecurityError('Immutable code/ACL changed')
            s=os.stat(r['node'].path,follow_symlinks=False)
            result.append((r['node'].path,s.st_ino,s.st_dev,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
        return tuple(result)
    def _file_records(self):
        # Cache only immutable artifact content/signatures in this process.
        # Fresh stat/ACL checks, kernel signatures and image inventory remain
        # mandatory on EVERY call. No serialized PASS or report is read.
        pin=self.artifacts.document.read();digest=hashlib.sha256(canonical(pin)).hexdigest()
        if 'artifacts' in self._documents and self._documents['artifacts']!=digest:raise SecurityError('Artifact pin changed within session')
        self._documents['artifacts']=digest
        if self._records is None:
            records=self.artifacts();self._stats=self._stable(records)
            for r in records:
                if r['kind']=='macho' and (r['signature_valid'] is not True or not isinstance(r['expected_team'],str) or not re.fullmatch('[A-Z0-9]{10}|APPLE_PLATFORM',r['expected_team']) or not isinstance(r['expected_cdhash'],str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',r['expected_cdhash']) or r['team']!=r['expected_team'] or r['cdhash']!=r['expected_cdhash']):raise SecurityError('Signature/Team/CDHash pin mismatch')
            self._records=records
        elif self._stable(self._records)!=self._stats:raise SecurityError('Code file replaced or changed')
        return self._records
    def _entitlements(self,path):
        if path in self._entitlements_checked:return
        rc,text=self.artifacts._command(['/usr/bin/codesign','-d','--entitlements',':-',path])
        if rc:raise SecurityError('Entitlements unavailable')
        start=text.find('<?xml');end=text.find('</plist>')
        if start<0:
            if text.strip() and ('<plist' in text or 'error' in text.lower()):raise SecurityError('Malformed entitlement evidence')
            self._entitlements_checked.add(path);return # no entitlement blob
        if end<start:raise SecurityError('Incomplete entitlement evidence')
        value=plistlib.loads(text[start:end+8].encode())
        if type(value) is not dict or any(value.get(k) not in (None,False) for k in DENIED_ENTITLEMENTS):raise SecurityError('Debug/JIT/library-validation bypass entitlement')
        self._entitlements_checked.add(path)
    def _load(self,records):
        if self._library is None:
            matches=[r for r in records if r['node'].path==PROBE]
            if len(matches)!=1 or matches[0]['kind']!='macho' or matches[0]['signature_valid'] is not True:raise SecurityError('Signed immutable native probe absent')
            self._library=C.CDLL(PROBE,use_errno=True)
            fn=self._library.aidrone_runtime_probe;fn.argtypes=[C.c_void_p,C.c_size_t];fn.restype=C.c_int
        return self._library
    def _probe(self,records):
        library=self._load(records);buffer=C.create_string_buffer(131072);size=library.aidrone_runtime_probe(buffer,len(buffer))
        if not 0<size<len(buffer):raise SecurityError('Current native runtime collection unavailable')
        from .spec import strict_json
        return strict_json(buffer.raw[:size],limit=131072)
    def _contains(self,path):
        fn=C.CDLL(None)._dyld_shared_cache_contains_path;fn.argtypes=[C.c_char_p];fn.restype=C.c_bool
        return bool(fn(path.encode()))
    def _verify(self,identity,layout,policy,raw,code,records,context=None):
        if identity.executable!=policy['executable']:raise SecurityError('Runtime executable policy mismatch')
        proof=validate_native(raw,policy,identity,self.clock(),current_kernel=code,contains=self._contains)
        normal=validate_code_closure(identity,records,verified_cache_paths=proof['cache_paths'])
        indexed={r['node'].path:r for r in records}
        if not proof['loaded_files']<=set(indexed):raise SecurityError('Loaded file absent from signed/hash closure')
        for image in raw['images']:
            if not image['cache'] and image['dylibs']!=list(indexed[image['path']]['dylibs']):raise SecurityError('Actual image dependencies differ from file pin')
        main=indexed.get(identity.executable)
        if main is None or main['cdhash']!=raw['cdhash']:raise SecurityError('Kernel CDHash and current signed file disagree')
        if context is not None:
            validate_runtime_context(context,records,verified_cache_paths=proof['cache_paths'])
            if set(context['native_images'])!={r['path'] for r in raw['images']}:raise SecurityError('Native image set changed after collection')
        if layout is not None:
            included={(r['node'].path,r['sha256']) for r in records}
            if not {(r['node'].path,r['sha256']) for role in ('code','interpreter','library') for r in layout[role]}<=included:raise SecurityError('Deployment differs from runtime pins')
        return normal
    def verify(self,identity,layout):
        try:
            if self.closed or type(identity) is not RuntimeIdentity or identity.pid!=os.getpid():raise SecurityError('Self runtime identity required')
            uid=self.spec.broker_uid if self.role=='broker' else self.spec.px4_uid
            if os.geteuid()!=uid or identity.uid!=uid:raise SecurityError('Runtime role is not active')
            before=read_kernel_identity(identity.pid);policy=self._policy(self.document,self.role);records=self._file_records()
            if (before['uid'],before['path'],before['birth'])!=(uid,identity.executable,identity.birth):raise SecurityError('Kernel birth/image mismatch')
            self._entitlements(identity.executable);raw=self._probe(records)
            result=self._verify(identity,layout,policy,raw,kernel_code(identity.pid),records,read_runtime_context())
            if read_kernel_identity(identity.pid)!=before:raise SecurityError('Process changed during collection')
            self.last_evidence=dict(scope='CURRENT_KERNEL_DYLD_ROOT_PINS',pid=identity.pid,birth=identity.birth,image_count=len(raw['images']),cache_uuid=raw['cache_uuid'],collected_at=raw['host_us']/1e6)
            return result
        except Exception:
            self.closed=True;self.last_evidence=None;raise SecurityError('macOS runtime trust UNVERIFIED') from None
    def verify_child(self,supervisor,raw):
        from .px4_supervisor import OwnedPX4Supervisor,PosixOwnedLauncher
        if self.role!='px4' or type(supervisor) is not OwnedPX4Supervisor or type(supervisor.launcher) is not PosixOwnedLauncher or supervisor.state!='STARTED':raise SecurityError('Only owned new child runtime allowed')
        try:
            before=supervisor.launcher.observe(supervisor.handle);policy=self._policy(self.child_document,'child');records=self._file_records()
            identity=RuntimeIdentity(before.pid,before.uid,before.birth,before.binary_sha256,self.spec.session_uuid,supervisor._approved_binary)
            self._entitlements(identity.executable)
            self._verify(identity,None,policy,raw,kernel_code(before.pid),records)
            if supervisor.launcher.observe(supervisor.handle)!=before:raise SecurityError('Child replaced during runtime evidence')
        except Exception:
            self.closed=True;raise SecurityError('Owned child runtime UNVERIFIED') from None


def self_socket_ownership(expected_paths):
    """Current SELF Unix endpoint ownership; no global/legacy PID enumeration.

    Connected/unnamed descriptors outside requested listeners are not an
    isolation PASS. No UDP is permitted for the IPC-only helper processes.
    """
    import subprocess
    if type(expected_paths) is not frozenset or not expected_paths or any(not p.startswith('/private/var/lib/aidrone/runtime/') or not p.endswith('/api.sock') for p in expected_paths):raise SecurityError('Pinned new endpoints required')
    pid=os.getpid();before=read_kernel_identity(pid)
    def query(kind):
        result=subprocess.run(['/usr/sbin/lsof','-nP','-a','-p',str(pid),kind,'-F0pftn'],capture_output=True,timeout=.5,shell=False,env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
        if result.stderr or len(result.stdout)>32768 or result.returncode not in (0,1):raise SecurityError('Self socket visibility unavailable')
        if result.returncode==1:
            if result.stdout:raise SecurityError('Partial socket output')
            return []
        process=None;rows=[];current=None
        for field in result.stdout.split(b'\0'):
            field=field.lstrip(b'\n')
            if not field:continue
            tag,value=chr(field[0]),field[1:].decode('ascii')
            if tag=='p':
                if process is not None or value!=str(pid):raise SecurityError('Socket process changed')
                process=pid
            elif tag=='f':
                if process!=pid or not re.fullmatch(r'[0-9]+[ruw]?',value):raise SecurityError('Socket PID/fd missing')
                if current is not None:rows.append(current)
                current={}
            elif tag in ('t','n'):
                if current is None or tag in current:raise SecurityError('Socket record malformed')
                current[tag]=value
            else:raise SecurityError('Unexpected socket field')
        if current is not None:rows.append(current)
        if process!=pid or not rows or any('t' not in r for r in rows):raise SecurityError('Current socket process/record absent')
        return rows
    rows=query('-U');names={r.get('n') for r in rows if r.get('t')=='unix'}
    if not expected_paths<=names or query('-iUDP'):raise SecurityError('Owned IPC endpoint missing or unexpected helper UDP')
    if read_kernel_identity(pid)!=before:raise SecurityError('Socket owner birth/UID changed')
    return dict(scope='CURRENT_SELF_REQUESTED_UNIX_NO_UDP',pid=pid,uid=before['uid'],birth=before['birth'],paths=tuple(sorted(expected_paths)))
