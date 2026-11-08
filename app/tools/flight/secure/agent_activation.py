"""Key-free Agent connect gate using OS-protected ROOT public approval files.

Agent receives no broker approval secret, P attestation secret, MAVLink key or
private manifest. Broker separately gates actual strict state before listening.
"""
from dataclasses import dataclass,replace
import hashlib
from threading import Lock
from uuid import uuid4 as new_uuid
from .activation import ActivationPermit,deployment_digest
from .access import ancestors,validate_node
from .identity_checks import CodeClosureChecker,RuntimeIdentity
from .platform_macos import checked_file
from .spec import SecurityError,canonical,finite,integer,private_path,strict_json,uuid4,LinkSpec

PUBLIC_FIELDS={'session_uuid','epoch','instance_id','vehicle_system','vehicle_component','controller_system','controller_component',
 'agent_uid','broker_uid','issued_at','expires_at','binary_sha256','policy_sha256','links','schema_version','backend','profile','capability'}


@dataclass(frozen=True)
class ObservationSessionView:
    session_uuid:str
    epoch:str
    agent_uid:int
    broker_uid:int
    issued_at:float
    expires_at:float
    digest:str
    public_bytes:bytes

    @classmethod
    def from_public(cls,value,digest):
        if not isinstance(value,dict) or set(value)!=PUBLIC_FIELDS:raise SecurityError('Exact public manifest required')
        uuid4(value['session_uuid']);uuid4(value['epoch'])
        if type(value['schema_version']) is not int or value['schema_version']!=1 or (value['backend'],value['profile'],value['capability'])!=('native','receiver_only_strict','monitor_only'):
            raise SecurityError('Public monitor-only profile required')
        integer(value['instance_id'],1,255)
        if value['instance_id'] in (1,7,218,219) or value['vehicle_system'] in (1,8,219,220):raise SecurityError('Legacy target rejected')
        for key in ('vehicle_system','vehicle_component','controller_system','controller_component'):integer(value[key],1,255)
        for key in ('agent_uid','broker_uid'):integer(value[key],1,2**31-1)
        if value['agent_uid']==value['broker_uid'] or value['vehicle_system']==value['controller_system']:raise SecurityError('Public role collision')
        start,end=finite(value['issued_at']),finite(value['expires_at'])
        if start<0 or not 0<end-start<=3600:raise SecurityError('Public session lifetime rejected')
        for data in (digest,value['binary_sha256'],value['policy_sha256']):
            if type(data) is not str or len(data)!=64 or any(c not in '0123456789abcdef' for c in data):raise SecurityError('Public digest required')
        links=value['links']
        if not isinstance(links,(tuple,list)) or len(links)!=2:raise SecurityError('Two public links required')
        try:parsed=[LinkSpec(**row) for row in links]
        except (TypeError,KeyError):raise SecurityError('Public link schema rejected') from None
        forbidden={10317,10318,13030,13280,14030,14280,14540,14550,14560,14561,14580,14641,14681,18570,19541,19581,19641,19681}
        if {p for row in parsed for p in (row.receiver_port,row.client_port)} & forbidden or len({p for row in parsed for p in (row.receiver_port,row.client_port)})!=4 or len({row.link_id for row in parsed})!=2:
            raise SecurityError('Public link collision')
        return cls(value['session_uuid'],value['epoch'],value['agent_uid'],value['broker_uid'],start,end,digest,canonical(value))

    def validate_time(self,now):
        if not self.issued_at<=finite(now)<self.expires_at:raise SecurityError('Public session expired')


class RootPublicDocument:
    """Owns the read path: does not accept JSON/CLI bytes as an authority.

    Only checked_file is patched in tests. Production reads no secret files.
    """
    def __init__(self,path,metadata,maximum=4096):
        self.path=private_path(path);self.metadata=metadata;self.maximum=integer(maximum,1,131072)
    def read(self):
        if not self.path.startswith('/Library/Application Support/AIDroneSecure/public/'):
            raise SecurityError('Root public authority path required')
        nodes=[]
        for path in (*ancestors(self.path),self.path):
            node=self.metadata(path)
            if node.path!=path:raise SecurityError('Root document path mismatch')
            if path==self.path:validate_node(node,uid=0,kind='file',mode=0o444)
            elif node.uid!=0 or node.kind!='dir' or node.mode&0o022 or node.acl:raise SecurityError('Unsafe root public ancestor')
            nodes.append(node)
        raw=checked_file(self.path,0,mode=0o444,maximum=self.maximum)
        if nodes[-1].size!=len(raw):raise SecurityError('Root public file size mismatch')
        after=[self.metadata(node.path) for node in nodes]
        if tuple(replace(node,captured_at=None) for node in nodes)!=tuple(replace(node,captured_at=None) for node in after):
            raise SecurityError('Root public document changed')
        return strict_json(raw,limit=self.maximum)


class AgentActivationProvider:
    def __init__(self,*,view,approval,manifest,identity_reader,endpoint,metadata_reader,code_checker,layout_reader,clock):
        if type(view) is not ObservationSessionView or type(approval) is not RootPublicDocument or type(manifest) is not RootPublicDocument or type(code_checker) is not CodeClosureChecker:
            raise SecurityError('Key-free Agent trust dependencies required')
        if approval.path!=f'/Library/Application Support/AIDroneSecure/public/{view.session_uuid}-agent-approval.json' or manifest.path!=f'/Library/Application Support/AIDroneSecure/public/{view.session_uuid}-manifest.json':
            raise SecurityError('Session-specific root public authority required')
        self.view,self.approval,self.manifest=view,approval,manifest
        self.identity,self.endpoint,self.metadata=identity_reader,endpoint,metadata_reader
        self.code,self.layout,self.clock=code_checker,layout_reader,clock
        self._issuer=object();self._used=set();self._created=0;self._grant_digest=None;self._lock=Lock();self.closed=False

    def close(self):self.closed=True
    def authorize(self,operation):
        from dataclasses import asdict
        with self._lock:
            try:
                if self.closed or self._created>=128 or operation!='connect_ipc':raise SecurityError('Agent can only connect once')
                self.view.validate_time(self.clock())
                grant=self.approval.read();manifest=self.manifest.read()
                digest=hashlib.sha256(canonical(grant)).hexdigest()
                if self._grant_digest is not None and digest!=self._grant_digest:raise SecurityError('Agent approval changed mid-session')
                fields={'version','approval_id','mode','session_uuid','epoch','spec_sha256','public_sha256','role_uid','identity','endpoint','issued_at','expires_at','deployment_sha256'}
                if set(grant)!=fields or type(grant['version']) is not int or grant['version']!=1:raise SecurityError('Agent approval schema rejected')
                uuid4(grant['approval_id']);now=finite(self.clock())
                start,end=finite(grant['issued_at']),finite(grant['expires_at'])
                if not start<=now<end or not 0<end-start<=300 or start<self.view.issued_at or end>self.view.expires_at:raise SecurityError('Agent approval expired')
                if grant['mode'] not in ('fixture','live'):raise SecurityError('Agent approval mode rejected')
                expected_public=hashlib.sha256(self.view.public_bytes).hexdigest()
                if ((grant['session_uuid'],grant['epoch'],grant['spec_sha256'],grant['public_sha256'],grant['role_uid'])
                        !=(self.view.session_uuid,self.view.epoch,self.view.digest,expected_public,self.view.agent_uid)
                        or type(grant['role_uid']) is not int or manifest!={'public':strict_json(self.view.public_bytes),'spec_sha256':self.view.digest}):
                    raise SecurityError('Agent session scope rejected')
                identity=self.identity()
                if type(identity) is not RuntimeIdentity or identity.uid!=self.view.agent_uid or identity.session_uuid!=self.view.session_uuid or grant['identity']!=asdict(identity):raise SecurityError('Agent running image rejected')
                if (self.endpoint.broker_uid,self.endpoint.agent_uid)!=(self.view.broker_uid,self.view.agent_uid) or grant['endpoint']!=self.endpoint.path:raise SecurityError('Agent endpoint scope rejected')
                layout=self.layout()
                if set(layout)!={'code','interpreter','library'} or deployment_digest(layout)!=grant['deployment_sha256']:raise SecurityError('Agent public deployment mismatch')
                self.code(identity,layout);self.endpoint.verify(self.metadata)
                if self.identity()!=identity:raise SecurityError('Agent image changed')
                self.view.validate_time(self.clock())
                if not start<=finite(self.clock())<end:raise SecurityError('Agent approval expired during validation')
                self._grant_digest=digest
                return ActivationPermit(operation,grant['mode'],min(end,finite(self.clock())+0.5),self._issuer,str(new_uuid()))
            except Exception:
                self.close();raise SecurityError('Agent connect blocked before socket creation') from None

    def consume_socket_permit(self,permit,operation,*,mode):
        with self._lock:
            if (self.closed or self._created>=128 or type(permit) is not ActivationPermit or permit.issuer is not self._issuer or permit in self._used
                    or operation!='connect_ipc' or permit.operation!=operation or permit.mode!=mode or finite(self.clock())>=permit.expires_at):
                self.close();raise SecurityError('Agent socket permit rejected')
            self._used.add(permit);self._created+=1
