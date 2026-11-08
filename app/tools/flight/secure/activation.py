"""Protected approval capability + deployment/current-state gate, no key loader.

HMAC authority secret is distinct from API/collector/MAVLink secrets. It is
shared only with trusted issuer/verifier; compromised Broker is out of scope.
Root-pinned secret digest/config must originate outside Agent-writable paths.
"""
from dataclasses import dataclass
import hashlib
from threading import Lock
from uuid import uuid4 as new_uuid
from .access import validate_node,validate_private_chain
from .attestation import read_node
from .deploy_policy import validate_layout
from .platform_macos import require_peer
from .policy import unseal
from .spec import SecurityError,canonical,finite,uuid4

ROLE_OPS={'agent':frozenset({'connect_ipc'}),'broker':frozenset({'listen_ipc','bind_loopback_udp','run_nonflight_trial'})}
GRANT_FIELDS={'version','approval_id','mode','session_uuid','epoch','spec_sha256','role','role_uid','identity',
              'endpoint','key_sha256','issued_at','expires_at','operations','deployment_sha256'}


def deployment_digest(layout):
    from dataclasses import asdict
    # Layout is private exact schema validated first; no keys included.
    value={role:[dict(path=r['node'].path,uid=r['node'].uid,mode=r['node'].mode,
                     sha256=r['sha256'],expected_sha256=r['expected_sha256']) for r in records]
           for role,records in layout.items()}
    return hashlib.sha256(canonical(value)).hexdigest()


class ProtectedApprovalTrust:
    """Construct only inside trusted bootstrap from protected FD/config readers.

    No file/environment/CLI loader or issuer exists. Metadata is collected by
    trusted adapters, never accepted as request JSON. Missing reader blocks.
    """
    def __init__(self,spec,secret,pin,metadata_reader):
        if type(secret) is not bytes or not 32<=len(secret)<=64 or not any(secret):raise SecurityError('Protected approval capability required')
        self._spec,self._secret,self._pin,self._reader=spec,secret,pin,metadata_reader

    def verify(self,raw):
        pin=self._pin()
        if set(pin)!={'node','secret_sha256'}:raise SecurityError('Approval trust pin missing')
        validate_node(pin['node'],uid=0,kind='file',mode=0o444)
        if pin['node'].path!='/Library/Application Support/AIDroneSecure/approval-pin.json':
            raise SecurityError('Trusted approval pin path required')
        # Root-owned pin ancestors must also be independently verified.
        from .access import ancestors
        for path in ancestors(pin['node'].path):
            node=self._reader(path)
            if node.path!=path or node.uid!=0 or node.kind!='dir' or node.mode&0o022 or node.acl:
                raise SecurityError('Unsafe approval pin ancestor')
        path='/Library/Application Support/AIDroneSecure/private/broker/approval.cap'
        validate_private_chain(path,self._reader,self._spec.broker_uid,leaf_kind='file',leaf_mode=0o600)
        if (type(self._secret) is not bytes or not any(self._secret) or len(self._secret)<32 or len(self._secret)>64
                or pin['secret_sha256']!=hashlib.sha256(self._secret).hexdigest()):
            raise SecurityError('Approval capability pin mismatch')
        return unseal(self._secret,raw)


@dataclass(frozen=True)
class ActivationPermit:
    operation:str
    mode:str
    expires_at:float
    issuer:object
    nonce:str


class TrustedActivationProvider:
    def __init__(self,*,spec,role,trust,grant_reader,identity_reader,manifest_reader,
                 layout_reader,endpoint,metadata_reader,attestation_exchange,attestation_verifier,
                 lease,key_fingerprint,clock,code_identity_check,reservation):
        from .identity_checks import CodeClosureChecker,validate_role_matrix
        validate_role_matrix(spec)
        from .durable import DurableClaims,ClaimLease
        from .attestation import AttestationVerifier
        if role!='broker' or type(trust) is not ProtectedApprovalTrust or type(code_identity_check) is not CodeClosureChecker or type(reservation) is not DurableClaims or type(lease) is not ClaimLease or type(attestation_verifier) is not AttestationVerifier:
            raise SecurityError('Trusted activation dependencies required')
        self.spec,self.role,self.trust=spec,role,trust
        self._grant_reader,self._identity_reader,self._manifest_reader=grant_reader,identity_reader,manifest_reader
        self._layout_reader,self.endpoint,self._metadata_reader=layout_reader,endpoint,metadata_reader
        self._exchange,self._verifier,self._lease=attestation_exchange,attestation_verifier,lease
        self._fingerprint,self.clock,self._code_check=key_fingerprint,clock,code_identity_check
        self._issuer=object();self._lock=Lock();self.closed=False;self._grant_id=None;self._grant_digest=None
        self._operations=0;self._lease_ready=False;self.last_state=None
        if (not any(attestation_verifier._token) or trust._secret==attestation_verifier._token or hashlib.sha256(trust._secret).digest()==key_fingerprint
                or hashlib.sha256(attestation_verifier._token).digest()==key_fingerprint):
            raise SecurityError('Approval/collector/MAVLink secrets must be distinct')
        self._reservation=reservation;self._used_permits=set();self._socket_counts={}
        self._listener_created=False

    def close(self):
        self.closed=True;self.last_state=None;self._lease.revoke()

    def authorize(self,operation):
        with self._lock:
            try:
                if self.closed or operation not in ROLE_OPS[self.role] or self._operations>=128:
                    raise SecurityError('Activation operation rejected')
                now=finite(self.clock());self.spec.validate_time(now)
                grant=self.trust.verify(self._grant_reader())
                if set(grant)!=GRANT_FIELDS or type(grant['version']) is not int or grant['version']!=1:
                    raise SecurityError('Approval schema rejected')
                digest=hashlib.sha256(canonical(grant)).hexdigest()
                if self._grant_digest is not None and digest!=self._grant_digest:raise SecurityError('Approval contents changed mid-session')
                uuid4(grant['approval_id'])
                if grant['mode'] not in ('fixture','live'):raise SecurityError('Approval mode rejected')
                if self._grant_id is not None and grant['approval_id']!=self._grant_id:raise SecurityError('Approval changed mid-session')
                issued,end=finite(grant['issued_at']),finite(grant['expires_at'])
                if not issued<=now<end or not 0<end-issued<=300 or issued<self.spec.issued_at or end>self.spec.expires_at:
                    raise SecurityError('Approval lifetime rejected')
                uid=self.spec.broker_uid if self.role=='broker' else self.spec.agent_uid
                if ((grant['session_uuid'],grant['epoch'],grant['spec_sha256'],grant['role'],grant['role_uid'])
                        !=(self.spec.session_uuid,self.spec.epoch,self.spec.digest,self.role,uid)
                        or type(grant['role_uid']) is not int):raise SecurityError('Approval scope rejected')
                from .identity_checks import RuntimeIdentity
                identity=self._identity_reader()
                if type(identity) is not RuntimeIdentity:raise SecurityError('Authoritative runtime identity required')
                from dataclasses import asdict
                if identity.uid!=uid or identity.session_uuid!=self.spec.session_uuid or identity.pid<=0 or not identity.birth or grant['identity']!=asdict(identity):
                    raise SecurityError('Bootstrap process identity mismatch')
                if grant['endpoint']!=self.endpoint.path:raise SecurityError('Endpoint approval mismatch')
                if self.role=='broker' and (self.endpoint.broker_uid,self.endpoint.agent_uid)!=(self.spec.broker_uid,self.spec.agent_uid):raise SecurityError('Endpoint identity mismatch')
                if grant['key_sha256']!=self._fingerprint.hex():raise SecurityError('Key reservation scope mismatch')
                if not isinstance(grant['operations'],list) or len(set(grant['operations']))!=len(grant['operations']) or not set(grant['operations'])<=ROLE_OPS[self.role] or operation not in grant['operations']:
                    raise SecurityError('Approved operation list rejected')
                manifest,path=self._manifest_reader()
                if manifest!=self.spec:raise SecurityError('Private manifest mismatch')
                validate_private_chain(path,self._metadata_reader,uid,leaf_kind='file',leaf_mode=0o600)
                if grant['mode']=='live':
                    from .runtime_contract import validate_runtime_contract
                    validate_runtime_contract(self.spec,self.endpoint,self._metadata_reader)
                layout=self._layout_reader();validate_layout(self.spec,layout)
                if deployment_digest(layout)!=grant['deployment_sha256']:raise SecurityError('Deployment grant mismatch')
                # A fresh authoritative code-sign/hash/dependency closure check,
                # not a declaration boolean, must return the exact result schema.
                code=self._code_check(identity,layout)
                if code!={'status':'PASS','scope':'VERIFIED_CODE_CLOSURE','binary_sha256':identity.binary_sha256}:
                    raise SecurityError('Code closure identity unverified')
                self._reservation.verify_lease(self._lease,self.spec,self._fingerprint)
                if operation=='listen_ipc' and not self._listener_created:
                    self.endpoint.verify_parent(self._metadata_reader)
                    try:self._metadata_reader(self.endpoint.path)
                    except FileNotFoundError:pass
                    else:raise SecurityError('Existing endpoint cannot be adopted')
                else:self.endpoint.verify(self._metadata_reader)
                challenge=str(new_uuid());peer,raw=self._exchange(challenge)
                state=self._verifier.verify(peer,raw,challenge)
                if state['key_sha256']!=self._fingerprint.hex():raise SecurityError('Collector key mismatch')
                if not self._lease_ready:
                    self._lease.confirm_px4(peer=peer,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,
                        key_sha256=state['key_sha256'],marker=read_node(state['marker']),parent=read_node(state['marker_parent']),now=self.clock())
                    self._lease_ready=True
                if self._identity_reader()!=identity:raise SecurityError('Bootstrap identity changed')
                self.spec.validate_time(self.clock())
                if not issued<=finite(self.clock())<end:raise SecurityError('Approval expired during validation')
                self._reservation.verify_lease(self._lease,self.spec,self._fingerprint)
                self.approval_expires_at=end
                self.last_state=state;self._grant_id=grant['approval_id'];self._grant_digest=digest;self._operations+=1
                return ActivationPermit(operation,grant['mode'],min(end,finite(self.clock())+0.5),self._issuer,str(new_uuid()))
            except Exception:
                self.close();raise SecurityError('Activation blocked before socket creation') from None

    def check_permit(self,permit,operation,*,mode):
        if (self.closed or type(permit) is not ActivationPermit or permit.issuer is not self._issuer
                or permit.operation!=operation or permit.mode!=mode or finite(self.clock())>=permit.expires_at):
            raise SecurityError('Activation permit rejected')
        return True

    def consume_socket_permit(self,permit,operation,*,mode):
        with self._lock:
            try:
                self.check_permit(permit,operation,mode=mode)
                limit={'listen_ipc':1,'bind_loopback_udp':2,'connect_ipc':1}[operation]
                if permit in self._used_permits or self._socket_counts.get(operation,0)>=limit:
                    raise SecurityError('Socket creation budget/replay rejected')
                self._used_permits.add(permit)
                self._socket_counts[operation]=self._socket_counts.get(operation,0)+1
                if operation=='listen_ipc':self._listener_created=True
            except Exception:
                self.close();raise SecurityError('Socket allocation blocked') from None
