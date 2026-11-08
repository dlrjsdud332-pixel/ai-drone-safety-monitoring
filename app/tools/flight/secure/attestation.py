"""Challenge-bound trusted P-side state contract, no PX4/IPC/OS commands.

Only trusted P adapters may supply readers; startup logs and stored reports are
not collection channels. OS identity and HMAC are independent prerequisites.
"""
from dataclasses import asdict
import hashlib
from threading import Lock
from .access import Node,validate_node,validate_udp_ownership
from .collector import evaluate_module_state
from .platform_macos import require_peer
from .policy import seal,unseal
from .session import OwnedIdentity,validate_observed_identity
from .spec import SecurityError,canonical,finite,integer,uuid4

ALLOWED_MODULES=frozenset({'secure_io','secure_telemetry','dataman','mavlink','param','perf','uorb','ver','shutdown','list_tasks','list_files','sleep'})
REQUIRED_MODULES=frozenset({'dataman','mavlink','param','uorb','ver'})
BODY_FIELDS={'version','session_uuid','epoch','spec_sha256','challenge','sequence','issued_at','ttl','state'}
STATE_FIELDS={'identity','instance_id','system_id','component_id','sockets','links','modules','marker','marker_parent','key_sha256','status_source','active_modules','collected_at','key_node','key_parent','manifest_node','manifest_parent','manifest_sha256','key_epoch_timestamp','reject_counts','query_id','status_channel'}


def node_dict(node):
    if node.acl:raise SecurityError('Private marker ACL rejected')
    value=asdict(node);value['acl']=[]
    return value


def read_node(value):
    if not isinstance(value,dict) or set(value)!={'path','uid','mode','kind','inode','device','nlink','acl','size','captured_at'} or value['acl']!=[]:
        raise SecurityError('Invalid marker metadata schema')
    return Node(**dict(value,acl=()))


def validate_state(spec,state,expected_identity,now,challenge=None):
    spec.validate_time(now)
    if not isinstance(state,dict) or set(state) not in (STATE_FIELDS,STATE_FIELDS|{'ipc_node','private_ancestors'}) or state['status_source']!='CURRENT_MODULE_QUERY':
        raise SecurityError('Current module state required')
    if not isinstance(state['identity'],dict) or set(state['identity'])!={'pid','uid','birth','binary_sha256','session_uuid'}:
        raise SecurityError('Process identity incomplete')
    uuid4(state['query_id'])
    if challenge is not None and state['query_id']!=challenge:raise SecurityError('Module query nonce mismatch')
    if state['status_channel'] not in ('IPC_STDOUT','PX4_LOG_QUERY_WINDOW','IN_PROCESS_MODULE_SNAPSHOT'):
        raise SecurityError('Current query channel unverified')
    integer(state['key_epoch_timestamp'],1,2**48-1)
    counts=state['reject_counts']
    if not isinstance(counts,dict) or set(counts)!={'unsigned','signature','replay','old','full','policy'}:
        raise SecurityError('Current rejection counters required')
    for count in counts.values():integer(count,0,2**63-1)
    if not 0<=finite(now)-finite(state['collected_at'])<=1:raise SecurityError('PX4 sources are stale')
    observed=OwnedIdentity(**state['identity'])
    integer(observed.pid,1,2**31-1)
    if not isinstance(observed.birth,str) or not observed.birth:raise SecurityError('Birth identity required')
    validate_observed_identity(spec,expected_identity,observed)
    for field,expected in (('instance_id',spec.instance_id),('system_id',spec.vehicle_system),('component_id',spec.vehicle_component)):
        if type(state[field]) is not int or state[field]!=expected:raise SecurityError('Receiver identity mismatch')
    if not isinstance(state['sockets'],list) or not isinstance(state['links'],list):raise SecurityError('Current link evidence required')
    validate_udp_ownership(spec,expected_identity,observed,state['sockets'],role='px4',captured_at=now,now=now)
    evaluate_module_state(spec,state['links'])
    modules=state['modules']
    if (not isinstance(modules,list) or any(type(m) is not str for m in modules) or len(set(modules))!=len(modules)
            or not REQUIRED_MODULES<=set(modules)<=ALLOWED_MODULES):
        raise SecurityError('Receiver-only module inventory required')
    active=state['active_modules']
    if not isinstance(active,list) or len(set(active))!=len(active) or not {'dataman','mavlink','uorb'}<=set(active)<=ALLOWED_MODULES:
        raise SecurityError('Running module inventory rejected')
    key=state['key_sha256']
    if type(key) is not str or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise SecurityError('Full key fingerprint required')
    parent=read_node(state['marker_parent']);marker=read_node(state['marker'])
    validate_node(parent,uid=spec.px4_uid,kind='dir',mode=0o700)
    validate_node(marker,uid=spec.px4_uid,kind='file',mode=0o600)
    if (parent.path!=spec.key_ledger_dir or marker.path!=parent.path+'/'+key or marker.size!=0
            or marker.captured_at is None or not 0<=finite(now)-finite(marker.captured_at)<=1):
        raise SecurityError('Fresh empty PX4 ledger marker required')
    key_node=read_node(state['key_node']);key_parent=read_node(state['key_parent'])
    manifest_node=read_node(state['manifest_node']);manifest_parent=read_node(state['manifest_parent'])
    for node,kind,mode in ((key_node,'file',0o600),(key_parent,'dir',0o700),(manifest_node,'file',0o600),(manifest_parent,'dir',0o700)):
        validate_node(node,uid=spec.px4_uid,kind=kind,mode=mode)
        if node.captured_at is None or not 0<=finite(now)-finite(node.captured_at)<=1:raise SecurityError('Private metadata stale')
    if (key_parent.path!=spec.runtime_dir+'/fs/mavlink' or key_node.path!=key_parent.path+'/mavlink-signing-key.bin'
            or key_node.size!=40 or manifest_parent.path!='/Library/Application Support/AIDroneSecure/private/px4'
            or manifest_node.path!=manifest_parent.path+'/manifest.json' or type(manifest_node.size) is not int or not 0<manifest_node.size<=16384 or state['manifest_sha256']!=spec.digest):
        raise SecurityError('Protected key/manifest metadata mismatch')
    if 'ipc_node' in state:
        ipc=read_node(state['ipc_node']);validate_node(ipc,uid=spec.px4_uid,kind='socket',mode=0o600)
        if ipc.path!=spec.runtime_dir+'/p.sock' or ipc.captured_at is None or not 0<=now-ipc.captured_at<=1:raise SecurityError('Current private IPC metadata rejected')
        rows=state['private_ancestors']
        if type(rows) is not list or len(rows)>64:raise SecurityError('Private ancestor inventory rejected')
        from .access import ancestors
        paths=set(ancestors(state['key_node']['path']))|set(ancestors(state['manifest_node']['path']))|set(ancestors(state['marker']['path']))|set(ancestors(ipc.path))
        seen=set()
        for value in rows:
            n=read_node(value)
            if n.path not in paths or n.path in seen or n.uid not in (0,spec.px4_uid) or n.kind!='dir' or n.mode&0o022 or n.acl or n.captured_at is None or not 0<=now-n.captured_at<=1:raise SecurityError('Private ancestor proof mismatch')
            seen.add(n.path)
        if seen!=paths:raise SecurityError('Private ancestor proof incomplete')
    return state


class PStateCollector:
    """A reader MUST requery identity/status/sockets; no default live reader.

    Reader takes a nonce and returns a complete current DTO. Calls before/after
    identity compare prevent PID reuse during collection. Errors produce no proof.
    """
    def __init__(self,spec,token,reader,identity_reader,clock):
        if type(token) is not bytes or not 32<=len(token)<=64 or not any(token):raise SecurityError('Independent collector capability required')
        self.spec,self._token,self._reader,self._identity_reader,self.clock=spec,token,reader,identity_reader,clock
        self._sequence=0;self._lock=Lock();self.closed=False

    def collect(self,challenge):
        with self._lock:
            try:
                if self.closed or self._sequence>=128:raise SecurityError('Collector unavailable/budget exhausted')
                uuid4(challenge);start=finite(self.clock());self.spec.validate_time(start)
                before=self._identity_reader()
                state=self._reader(challenge)
                after=self._identity_reader();now=finite(self.clock());self.spec.validate_time(now)
                if before!=after or not 0<=now-start<=1:raise SecurityError('Collection identity/time changed')
                validate_state(self.spec,state,before,now,challenge)
                self._sequence+=1
                return seal(self._token,dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,
                    spec_sha256=self.spec.digest,challenge=challenge,sequence=self._sequence,issued_at=now,ttl=1.,state=state))
            except Exception:
                self.closed=True
                raise SecurityError('P-side collection unavailable') from None


class AttestationVerifier:
    def __init__(self,spec,token,expected_identity,clock):
        if type(token) is not bytes or not 32<=len(token)<=64 or not any(token):raise SecurityError('Protected verifier capability required')
        self.spec,self._token,self.expected,self.clock=spec,token,expected_identity,clock
        self._sequence=0;self._challenges=set();self._reject_counts=None;self._lock=Lock();self.closed=False

    def verify(self,peer,raw,challenge):
        with self._lock:
            try:
                if self.closed:raise SecurityError('Attestation unavailable')
                require_peer(peer,self.spec.px4_uid);uuid4(challenge)
                from .collector_wire import unseal_state
                value=unseal_state(self._token,raw)
                if set(value)!=BODY_FIELDS or type(value['version']) is not int or value['version']!=1:
                    raise SecurityError('Attestation envelope rejected')
                if (value['session_uuid'],value['epoch'],value['spec_sha256'],value['challenge'])!=(self.spec.session_uuid,self.spec.epoch,self.spec.digest,challenge):
                    raise SecurityError('Attestation correlation failed')
                now=finite(self.clock());self.spec.validate_time(now)
                issued,ttl=finite(value['issued_at']),finite(value['ttl'])
                if not 0<ttl<=1 or issued<self.spec.issued_at or not issued<=now<issued+ttl:raise SecurityError('Attestation expired')
                sequence=integer(value['sequence'],1,2**53-1)
                if sequence<=self._sequence or challenge in self._challenges or len(self._challenges)>=128:
                    raise SecurityError('Attestation replay/budget rejected')
                validate_state(self.spec,value['state'],self.expected,now,challenge)
                counts=value['state']['reject_counts']
                if self._reject_counts is not None and any(counts[key]<self._reject_counts[key] for key in counts):
                    raise SecurityError('P counters rolled back')
                self._reject_counts=dict(counts)
                self._sequence=sequence;self._challenges.add(challenge)
                return value['state']
            except Exception:
                self.closed=True
                raise SecurityError('Current PX4 state UNVERIFIED') from None


class PQueryAdapter:
    """Narrow in-process P-side reader assembly; no arbitrary command API.

    Installed adapters provide current status/app inventory/socket/marker readers.
    They may not substitute startup logs or saved reports. No default OS command
    runner or IPC socket is supplied in this offline phase.
    """
    def __init__(self,spec,*,identity,links,sockets,compiled_apps,running_modules,marker,private_files,reject_counts,clock,uid_reader,status_channel='IN_PROCESS_MODULE_SNAPSHOT'):
        self.spec=spec;self.identity=identity;self.links=links;self.sockets=sockets
        self.apps=compiled_apps;self.running=running_modules;self.marker=marker;self.clock=clock;self.uid=uid_reader;self.private_files=private_files;self.reject_counts=reject_counts;self.channel=status_channel

    def __call__(self,challenge):
        uuid4(challenge)
        if self.uid()!=self.spec.px4_uid:raise SecurityError('P-side account required')
        before=self.identity();now=finite(self.clock());self.spec.validate_time(now)
        links=self.links();sockets=self.sockets();apps=self.apps();running=self.running()
        key,parent,marker,key_epoch=self.marker()
        if self.identity()!=before:raise SecurityError('P process changed during query')
        return dict(identity=asdict(before),instance_id=self.spec.instance_id,system_id=self.spec.vehicle_system,
            component_id=self.spec.vehicle_component,sockets=sockets,links=links,modules=apps,
            active_modules=running,marker=node_dict(marker),marker_parent=node_dict(parent),key_sha256=key,
            status_source='CURRENT_MODULE_QUERY',collected_at=now,query_id=challenge,status_channel=self.channel,
            reject_counts=self.reject_counts(),key_epoch_timestamp=key_epoch,**self.private_files())
