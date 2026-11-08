"""Read-only inventory/signature checks and exact immutable code policy.

No account, group, ACL, signing, sudo or service mutation operations exist here.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
from .access import ancestors,validate_node
from .spec import SecurityError,integer,private_path


@dataclass(frozen=True)
class RuntimeIdentity:
    pid:int
    uid:int
    birth:str
    binary_sha256:str
    session_uuid:str
    executable:str


def validate_role_matrix(spec):
    if spec.agent_uid!=501 or 501 in (spec.broker_uid,spec.px4_uid) or len({spec.agent_uid,spec.broker_uid,spec.px4_uid})!=3:
        raise SecurityError('Existing UID501 remains Agent; dedicated B/P UIDs required')


def validate_account_inventory(spec,users,groups,*,phase='preinstall'):
    validate_role_matrix(spec)
    if phase not in ('preinstall','installed'):raise SecurityError('Inventory phase rejected')
    if not isinstance(users,list) or not users or not isinstance(groups,list) or not groups:raise SecurityError('Complete directory inventory required')
    for row in users:
        if set(row)!={'name','uid','gid','groups','home','shell'}:raise SecurityError('User inventory schema rejected')
        integer(row['uid'],-2,2**32-1);integer(row['gid'],-2,2**32-1)
        if not isinstance(row['groups'],list):raise SecurityError('Membership inventory missing')
    for row in groups:
        if set(row)!={'name','gid','members'}:raise SecurityError('Group inventory schema rejected')
        integer(row['gid'],-2,2**32-1)
    agent=[r for r in users if r['uid']==spec.agent_uid]
    if len(agent)!=1:raise SecurityError('Existing Agent identity not established')
    roles={'_aidrone_broker':spec.broker_uid,'_aidrone_px4':spec.px4_uid}
    if agent[0]['gid'] in roles.values() or any(name in agent[0]['groups'] for name in roles):raise SecurityError('Agent cannot inherit service groups')
    for name,uid in roles.items():
        if uid<=500:raise SecurityError('Dedicated non-system service identity required')
        same=[r for r in users if r['name']==name or r['uid']==uid]
        selected=[r for r in groups if r['name']==name or r['gid']==uid]
        if phase=='preinstall':
            if same or selected:raise SecurityError('Requested role UID/name/GID collision')
        else:
            if (len(same)!=1 or same[0]['name']!=name or same[0]['uid']!=uid or same[0]['gid']!=uid
                    or same[0]['groups']!=[name] or same[0]['home']!='/var/empty' or same[0]['shell']!='/usr/bin/false'
                    or len(selected)!=1 or selected[0]['name']!=name or selected[0]['gid']!=uid
                    or selected[0]['members'] not in ([],[name])):
                raise SecurityError('Installed service role boundary rejected')
    return dict(status='PASS',scope='READONLY_INVENTORY_'+phase.upper(),applied=False)


def read_account_inventory():
    """Explicit read-only function, never invoked at import or by offline runner.

    Directory service unavailability raises; never treat missing data as no users.
    """
    import pwd,grp
    try:
        directory=list(pwd.getpwall()); group_rows=list(grp.getgrall())
        if not directory or not group_rows:raise SecurityError('Directory service inventory unavailable')
        users=[dict(name=r.pw_name,uid=r.pw_uid,gid=r.pw_gid,
                    groups=sorted({g.gr_name for g in group_rows if r.pw_name in g.gr_mem or r.pw_gid==g.gr_gid}),
                    home=r.pw_dir,shell=r.pw_shell) for r in directory]
        groups=[dict(name=r.gr_name,gid=r.gr_gid,members=list(r.gr_mem)) for r in group_rows]
        return users,groups
    except Exception:raise SecurityError('Read-only account inventory unavailable') from None


def validate_code_closure(identity,records,*,verified_cache_paths=frozenset()):
    if not isinstance(identity,RuntimeIdentity) or not isinstance(records,tuple) or not records:raise SecurityError('Typed code closure evidence required')
    integer(identity.pid,1,2**31-1)
    if type(identity.birth) is not str or not identity.birth:raise SecurityError('Birth identity required')
    paths=set();dependencies=set();main=False
    required={'node','ancestors','sha256','expected_sha256','kind','signature_valid','team','expected_team','cdhash','expected_cdhash','dylibs','expected_dylibs'}
    for record in records:
        if not isinstance(record,dict) or set(record)!=required:raise SecurityError('Code closure schema rejected')
        node=record['node'];private_path(node.path)
        if node.path in paths or not node.path.startswith(('/Library/Application Support/AIDroneSecure/','/usr/lib/','/System/Library/')):
            raise SecurityError('Code closure path rejected')
        if node.mode not in (0o444,0o555,0o644,0o755) or node.mode&0o022:raise SecurityError('Writable code artifact rejected')
        validate_node(node,uid=0,kind='file',mode=node.mode)
        if tuple(p.path for p in record['ancestors'])!=ancestors(node.path):raise SecurityError('Complete code ancestors required')
        for parent in record['ancestors']:
            if parent.uid!=0 or parent.kind!='dir' or parent.mode&0o022 or parent.acl:raise SecurityError('Writable code ancestor rejected')
        expected=record['expected_sha256']
        if type(expected) is not str or not re.fullmatch('[0-9a-f]{64}',expected) or record['sha256']!=expected:
            raise SecurityError('Code hash mismatch')
        if record['kind']=='macho':
            cdhash=record['expected_cdhash'];team=record['expected_team']
            if (record['signature_valid'] is not True or type(team) is not str or not re.fullmatch('[A-Z0-9]{10}|APPLE_PLATFORM',team)
                    or record['team']!=team or type(cdhash) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',cdhash)
                    or record['cdhash']!=cdhash):raise SecurityError('Code signing policy unverified')
        elif record['kind']=='script':
            if any(record[name] is not None for name in ('signature_valid','team','expected_team','cdhash','expected_cdhash')):
                raise SecurityError('Script signing claim rejected')
        else:raise SecurityError('Unknown code kind')
        if type(record['dylibs']) is not tuple or record['dylibs']!=record['expected_dylibs']:
            raise SecurityError('Dylib closure mismatch')
        if any(not isinstance(p,str) or not p.startswith(('/Library/Application Support/AIDroneSecure/','/usr/lib/','/System/Library/')) for p in record['dylibs']):
            raise SecurityError('Unresolved/injected dylib dependency')
        dependencies.update(record['dylibs']);paths.add(node.path)
        if node.path==identity.executable:
            if record['kind']!='macho' or record['sha256']!=identity.binary_sha256 or node.mode not in (0o555,0o755):raise SecurityError('Running image mismatch')
            main=True
    if not main or not dependencies<=paths|set(verified_cache_paths):raise SecurityError('Executable or transitive dependency missing')
    return dict(status='PASS',scope='VERIFIED_CODE_CLOSURE',binary_sha256=identity.binary_sha256)


def read_runtime_context():
    import sys
    loaded=tuple(sorted({str(Path(module.__file__).resolve()) for module in tuple(sys.modules.values())
                         if getattr(module,'__file__',None)}))
    import ctypes
    if sys.platform!='darwin':raise SecurityError('Native image closure unavailable')
    library=ctypes.CDLL(None)
    count=library._dyld_image_count;count.argtypes=[];count.restype=ctypes.c_uint32
    name=library._dyld_get_image_name;name.argtypes=[ctypes.c_uint32];name.restype=ctypes.c_char_p
    size=count()
    if size>1024:raise SecurityError('Native image budget exceeded')
    images=[]
    for index in range(size):
        value=name(index)
        if not value:raise SecurityError('Native image name unavailable')
        images.append(value.decode())
    if count()!=size:raise SecurityError('Native image set changed during collection')
    return dict(environment=dict(os.environ),search_paths=tuple(sys.path),loaded_files=loaded,native_images=tuple(images))


def validate_runtime_context(context,records,*,verified_cache_paths=frozenset()):
    if not isinstance(context,dict) or set(context)!={'environment','search_paths','loaded_files','native_images'}:
        raise SecurityError('Complete runtime context required')
    environment=context['environment']
    if not isinstance(environment,dict) or environment.get('PATH')!='/usr/bin:/bin' or environment.get('PYTHONNOUSERSITE')!='1':
        raise SecurityError('Trusted isolated runtime environment required')
    for name,value in environment.items():
        if value and (name.startswith('DYLD_') or name in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','PYTHONUSERBASE','LD_PRELOAD','LD_LIBRARY_PATH','CONDA_PREFIX','VIRTUAL_ENV','PYTHONINSPECT','PYTHONBREAKPOINT','OPENSSL_CONF','OPENSSL_MODULES','SSLKEYLOGFILE')):
            raise SecurityError('Runtime environment injection rejected')
    roots=('/Library/Application Support/AIDroneSecure/','/usr/lib/','/System/Library/')
    paths=context['search_paths'];loaded=context['loaded_files'];images=context['native_images']
    if type(paths) is not tuple or not paths or type(loaded) is not tuple or not loaded or type(images) is not tuple or not images:
        raise SecurityError('Complete module load context required')
    for path in paths+loaded+images:
        private_path(path)
        if not path.startswith(roots):raise SecurityError('Untrusted runtime import path')
    inventory={record['node'].path for record in records}|set(verified_cache_paths)
    if not (set(loaded)|set(images))<=inventory:raise SecurityError('Loaded code absent from pinned closure')


class CodeClosureChecker:
    def __init__(self,reader,runtime_reader=None,*,native_evidence=None):
        if native_evidence is not None:
            from .macos_runtime_evidence import MacRuntimeEvidence
            if type(native_evidence) is not MacRuntimeEvidence:raise SecurityError("Typed native runtime source required")
        self.native_evidence=native_evidence
        self.reader=reader;self.runtime_reader=runtime_reader or read_runtime_context
    def __call__(self,identity,layout):
        if self.native_evidence is not None:return self.native_evidence.verify(identity,layout)
        records=self.reader()
        proof=validate_code_closure(identity,records)
        validate_runtime_context(self.runtime_reader(),records)
        included={(r['node'].path,r['sha256']) for r in records}
        expected={(r['node'].path,r['sha256']) for role in ('code','interpreter','library') for r in layout[role]}
        if not expected<=included:raise SecurityError('Deployment/runtime code closure mismatch')
        return proof


def inspect_macho(path,runner=subprocess.run):
    """Read-only developer artifact observation, never a readiness grant.

    No codesign signing command exists; actual loaded shared-cache images and
    production designated requirements still need deployment-time collection.
    """
    path=Path(path);private_path(str(path))
    if path.name!='px4' or not path.is_file():raise SecurityError('Independent binary observation target required')
    commands=[['/usr/bin/codesign','-dv','--verbose=4',str(path)],
              ['/usr/bin/codesign','--verify','--strict',str(path)],
              ['/usr/bin/otool','-L',str(path)],['/usr/bin/file',str(path)]]
    rows=[]
    for command in commands:
        try:
            result=runner(command,capture_output=True,text=True,timeout=10,shell=False)
            rows.append(dict(command=command,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
        except (OSError,subprocess.SubprocessError):rows.append(dict(command=command,status='BLOCKED'))
    text='\n'.join(row.get('stdout','')+row.get('stderr','') for row in rows)
    kind='ADHOC' if 'Signature=adhoc' in text else 'SIGNED_METADATA' if 'Authority=' in text else 'UNVERIFIED'
    return dict(status='PASS' if all('returncode' in row for row in rows) else 'BLOCKED',
        scope='DEVELOPER_BINARY_READONLY_OBSERVATION',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        signature_kind=kind,readiness='UNVERIFIED',production_activation='BLOCKED',commands=rows,
        binary_executed=False)


class CurrentIdentityReader:
    """Explicit self-only read adapter. No argument/ENV binary identity claims.

    UID gate precedes any OS query. proc_pidpath obtains the kernel image path;
    ps reads this process birth only. Missing permissions never fall back to argv.
    """
    def __init__(self,spec,role,executable,expected_hash,metadata_reader):
        if role not in ('agent','broker','px4'):raise SecurityError('Role required')
        self.spec,self.role,self.executable,self.expected_hash,self.metadata=spec,role,executable,expected_hash,metadata_reader

    def __call__(self):
        try:
            uid={'agent':self.spec.agent_uid,'broker':self.spec.broker_uid,'px4':self.spec.px4_uid}[self.role]
            if os.geteuid()!=uid:raise SecurityError('OS role is not active')
            private_path(self.executable)
            if not self.executable.startswith('/Library/Application Support/AIDroneSecure/'):
                raise SecurityError('Trusted runtime image required')
            snapshot=read_kernel_identity(os.getpid())
            pid=snapshot['pid']
            if snapshot['path']!=self.executable or snapshot['uid']!=uid:raise SecurityError('Kernel executable identity mismatch')
            node=self.metadata(self.executable)
            if node.uid!=0 or node.kind!='file' or node.mode&0o022 or node.acl:raise SecurityError('Unsafe runtime image')
            digest=hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()
            if digest!=self.expected_hash:raise SecurityError('Runtime executable hash mismatch')
            after=read_kernel_identity(pid)
            if after!=snapshot:raise SecurityError('Current process changed during observation')
            return RuntimeIdentity(pid,uid,snapshot['birth'],digest,self.spec.session_uuid,self.executable)
        except Exception:raise SecurityError('Current runtime identity UNVERIFIED') from None



def read_kernel_identity(pid):
    """Darwin libproc read only. Callers restrict PID to self/registered child.

    ABI is from installed sys/proc_info.h PROC_PIDTBSDINFO, no ps fallback.
    """
    import ctypes as C
    import sys
    integer(pid,1,2**31-1)
    if sys.platform!='darwin':raise SecurityError('Darwin process identity unavailable')
    class BSDInfo(C.Structure):
        _fields_=[(name,C.c_uint32) for name in ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]+[
            ('comm',C.c_char*16),('name',C.c_char*32)]+[(name,C.c_uint32) for name in ('nfiles','pgid','jobc','tdev','tpgid')]+[
            ('nice',C.c_int32),('start_sec',C.c_uint64),('start_usec',C.c_uint64)]
    try:
        library=C.CDLL('/usr/lib/libproc.dylib',use_errno=True)
        read=library.proc_pidinfo;read.argtypes=[C.c_int,C.c_int,C.c_uint64,C.c_void_p,C.c_int];read.restype=C.c_int
        path=library.proc_pidpath;path.argtypes=[C.c_int,C.c_void_p,C.c_uint32];path.restype=C.c_int
        before=BSDInfo();after=BSDInfo();buffer=C.create_string_buffer(4096)
        if read(pid,3,0,C.byref(before),C.sizeof(before))!=C.sizeof(before) or path(pid,buffer,len(buffer))<=0 or read(pid,3,0,C.byref(after),C.sizeof(after))!=C.sizeof(after):
            raise SecurityError('Kernel process snapshot unavailable')
        keys=('pid','ppid','uid','start_sec','start_usec')
        if any(getattr(before,key)!=getattr(after,key) for key in keys) or before.pid!=pid or before.start_sec==0 or before.start_usec>=1000000:
            raise SecurityError('Kernel process snapshot unstable')
        return dict(pid=pid,ppid=before.ppid,uid=before.uid,birth=f'{before.start_sec}.{before.start_usec:06d}',path=buffer.value.decode())
    except Exception:raise SecurityError('Kernel identity UNVERIFIED') from None
