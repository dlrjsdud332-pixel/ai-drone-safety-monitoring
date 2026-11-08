"""P-owned export endpoint + authenticated B pull. No automatic service startup."""
from dataclasses import asdict
import os
import socket
from threading import Lock
from uuid import uuid4 as new_uuid
from .spec import SecurityError,finite,integer,uuid4,canonical
from .runtime_contract import runtime_paths,CODE_ROOT
from .execution import Endpoint,Connector,Listener,Deadline,DeadlineStream,_PrivateListener
from .platform_macos import PeerCredentials,require_peer
from .agent_activation import RootPublicDocument
from .identity_checks import CodeClosureChecker,RuntimeIdentity,CurrentIdentityReader
from .activation import TrustedActivationProvider
from .px4_supervisor import OwnedPX4Supervisor,PosixOwnedLauncher
from .service_entry import ServiceContext
from .session import validate_observed_identity,OwnedIdentity
from .policy import seal,unseal
from .ipc_transport import receive_frame,send_frame


def export_endpoint(spec):
    return Endpoint(runtime_paths(spec)['root']+'/c/api.sock',spec.px4_uid,spec.broker_uid)


class ExportChannelGate:
    """Root document + current immutable identity + original start/ledger gates.

    The P endpoint is a sibling c/ directory, not inside P's key-containing p/.
    B gets search/read-write ACL on c/api.sock only; Agent gets none.
    """
    def __init__(self,context,document,identity,code_check,layout,metadata,clock,*,supervisor=None,broker_provider=None,uid_reader=os.geteuid):
        if type(context) is not ServiceContext or context.role not in ('px4','broker') or type(document) is not RootPublicDocument or type(code_check) is not CodeClosureChecker:raise SecurityError('Prepared exporter service required')
        spec=context.spec
        from .identity_checks import validate_role_matrix
        validate_role_matrix(spec)
        if document.path!=CODE_ROOT+'/public/'+spec.session_uuid+'-telemetry-channel.json':raise SecurityError('Fixed export authority required')
        if context.role=='px4' and type(supervisor) is not OwnedPX4Supervisor:raise SecurityError('Owned P supervisor required')
        if context.role=='broker' and type(broker_provider) is not TrustedActivationProvider:raise SecurityError('Broker activation required')
        self.context,self.spec,self.document,self.identity,self.code_check,self.layout,self.metadata,self.clock=context,spec,document,identity,code_check,layout,metadata,clock
        self.supervisor,self.provider,self.uid=supervisor,broker_provider,uid_reader
        self.endpoint=export_endpoint(spec);self.closed=False;self.digest=None;self.count=0;self.allocations=set();self.lock=Lock()
    def authorize(self,operation):
        with self.lock:
            try:
                if self.closed or self.count>=512:raise SecurityError('Export service unavailable')
                role=self.context.role;allowed={'listen_export','export_snapshot'} if role=='px4' else {'connect_export'}
                if operation not in allowed:raise SecurityError('Fixed export operation required')
                now=finite(self.clock());self.spec.validate_time(now);grant=self.document.read()
                fields={'version','approval_id','mode','session_uuid','epoch','spec_sha256','endpoint','p_helper','b_helper','child','issued_at','expires_at'}
                if set(grant)!=fields or type(grant['version']) is not int or grant['version']!=1 or grant['mode'] not in ('fixture','live'):raise SecurityError('Export authority rejected')
                uuid4(grant['approval_id'])
                if (grant['session_uuid'],grant['epoch'],grant['spec_sha256'],grant['endpoint'])!=(self.spec.session_uuid,self.spec.epoch,self.spec.digest,self.endpoint.path):raise SecurityError('Export authority scope rejected')
                issued,end=finite(grant['issued_at']),finite(grant['expires_at'])
                if not self.spec.issued_at<=issued<=now<end<=self.spec.expires_at or not 0<end-issued<=300:raise SecurityError('Export authority expired')
                digest=canonical(grant)
                if self.digest is not None and digest!=self.digest:raise SecurityError('Export authority changed')
                uid=self.spec.px4_uid if role=='px4' else self.spec.broker_uid
                identity=self.identity()
                if type(identity) is not RuntimeIdentity or identity.uid!=uid or self.uid()!=uid or identity.session_uuid!=self.spec.session_uuid or asdict(identity)!=grant['p_helper' if role=='px4' else 'b_helper']:raise SecurityError('Export helper identity rejected')
                proof=self.code_check(identity,self.layout())
                if proof!={'status':'PASS','scope':'VERIFIED_CODE_CLOSURE','binary_sha256':identity.binary_sha256}:raise SecurityError('Export immutable closure unavailable')
                child=OwnedIdentity(**grant['child']);validate_observed_identity(self.spec,child,child)
                if role=='px4':
                    s=self.supervisor
                    if s.context.spec!=self.spec or s.state!='STARTED' or s.expected!=child or s.launcher.observe(s.handle)!=child:raise SecurityError('Owned export child changed')
                    if grant['mode']=='live' and (type(s.launcher) is not PosixOwnedLauncher or type(self.identity) is not CurrentIdentityReader):raise SecurityError('Live P kernel sources required')
                else:
                    if self.provider.spec!=self.spec or self.provider._verifier.expected!=child:raise SecurityError('Broker/P identity mismatch')
                    permit=self.provider.authorize('listen_ipc')
                    if permit.mode!=grant['mode'] or 'secure_telemetry' not in self.provider.last_state['modules']:raise SecurityError('Exporter binary/current P proof required')
                if grant['mode']=='live':
                    if os.geteuid()!=uid:raise SecurityError('Active OS export role required')
                    from .runtime_contract import validate_runtime_contract
                    validate_runtime_contract(self.spec,Endpoint(runtime_paths(self.spec)['api'],self.spec.broker_uid,self.spec.agent_uid),self.metadata)
                end_now=finite(self.clock());self.spec.validate_time(end_now)
                if not issued<=end_now<end or self.identity()!=identity:raise SecurityError('Export authority expired/identity changed during validation')
                self.digest=digest;self.count+=1
                return grant['mode']
            except Exception:
                self.closed=True;raise SecurityError('Export activation blocked') from None
    def allocate(self,operation):
        mode=self.authorize(operation)
        with self.lock:
            if self.closed or (operation=='listen_export' and operation in self.allocations):raise SecurityError('Export allocation unavailable')
            self.allocations.add(operation)
        return mode
    def close(self):self.closed=True


class ExportSocketBackend:
    """Only an exact gate and root-approved live mode may create a socket."""
    def __init__(self,gate,*,fixture_backend=None,acl_installer=None):
        if type(gate) is not ExportChannelGate:raise SecurityError('Export gate required')
        self.gate,self.fixture,self.acl=gate,fixture_backend,acl_installer
    def _create(self,operation,kind):
        raw=None
        try:
            mode=self.gate.allocate(operation)
            if mode=='fixture':
                if self.fixture is None:raise SecurityError('Memory export backend required')
                return getattr(self.fixture,kind)()
            if kind=='listener' and self.acl is None:raise SecurityError('Approved endpoint ACL installer unavailable')
            raw=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            return _PrivateListener(raw,self.acl) if kind=='listener' else raw
        except Exception:
            if raw is not None:raw.close()
            self.gate.close();raise SecurityError('Export socket blocked') from None
    def listener(self):return self._create('listen_export','listener')
    def stream(self):return self._create('connect_export','stream')


class ExportRPCServer:
    def __init__(self,spec,control_token,collector,gate,credentials=PeerCredentials.from_connected_socket):
        import hmac
        if type(control_token) is not bytes or len(control_token)!=32 or not any(control_token) or hmac.compare_digest(control_token,collector.token):raise SecurityError('Independent export control capability required')
        if collector.spec!=spec or type(gate) is not ExportChannelGate or gate.context.role!='px4':raise SecurityError('P exporter bindings required')
        self.spec,self.token,self.collector,self.gate,self.credentials=spec,control_token,collector,gate,credentials
        self.sequence=0;self.ids=set();self.closed=False;self.lock=Lock()
    def handle(self,peer,raw):
        if not self.lock.acquire(blocking=False):raise SecurityError('Export concurrency exceeded')
        try:
            if self.closed:raise SecurityError('Export server closed')
            self.gate.authorize('export_snapshot');require_peer(peer,self.spec.broker_uid);v=unseal(self.token,raw);now=finite(self.collector.clock())
            if set(v)!={'version','operation','session_uuid','epoch','nonce','request_id','sequence','issued_at','ttl'} or type(v['version']) is not int or v['version']!=1 or v['operation']!='snapshot':raise SecurityError('Export request rejected')
            if (v['session_uuid'],v['epoch'])!=(self.spec.session_uuid,self.spec.epoch):raise SecurityError('Export request session mismatch')
            uuid4(v['nonce']);uuid4(v['request_id']);seq=integer(v['sequence'],1,128);issued=finite(v['issued_at']);ttl=finite(v['ttl'])
            if seq!=self.sequence+1 or v['request_id'] in self.ids or not 0<ttl<=1 or not self.spec.issued_at<=issued<=now<issued+ttl:raise SecurityError('Export request replay/expiry')
            self.sequence=seq;self.ids.add(v['request_id']);body=self.collector.collect(v['nonce'],deadline=issued+ttl)
            self.gate.authorize('export_snapshot');end=finite(self.collector.clock())
            if not now<=end<issued+ttl:raise SecurityError('Export transaction expired')
            return seal(self.token,dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,nonce=v['nonce'],request_id=v['request_id'],sequence=seq,issued_at=end,ttl=issued+ttl-end,telemetry=body.decode()))
        except Exception:
            self.close();raise SecurityError('Export RPC blocked') from None
        finally:self.lock.release()
    def handle_connected(self,stream):
        try:send_frame(stream,self.handle(self.credentials(stream),receive_frame(stream)))
        except Exception:self.close();raise SecurityError('Export stream blocked') from None
        finally:stream.close()
    def close(self):self.closed=True;self.collector.closed=True;self.gate.close()


class ExportRPCClient:
    def __init__(self,spec,token,connector,gate,clock):
        if type(token) is not bytes or len(token)!=32 or not any(token) or type(connector) is not Connector or type(gate) is not ExportChannelGate or gate.context.role!='broker':raise SecurityError('B exporter client bindings required')
        self.spec,self.token,self.connector,self.gate,self.clock=spec,token,connector,gate,clock
        self.closed=False;self.sequence=0;self.lock=Lock()
    def __call__(self,nonce):
        stream=None
        if not self.lock.acquire(blocking=False):raise SecurityError('Export client busy')
        try:
            if self.closed:raise SecurityError('Export client closed')
            uuid4(nonce);self.gate.authorize('connect_export');now=finite(self.clock());self.spec.validate_time(now);budget=Deadline(1,clock=self.clock)
            self.sequence=integer(self.sequence+1,1,128);rid=str(new_uuid())
            raw=seal(self.token,dict(version=1,operation='snapshot',session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,nonce=nonce,request_id=rid,sequence=self.sequence,issued_at=now,ttl=1.))
            # One outer deadline includes connect, credentials and all frame I/O.
            stream=DeadlineStream(self.connector(deadline=budget),budget);peer=PeerCredentials.from_connected_socket(stream);require_peer(peer,self.spec.px4_uid)
            send_frame(stream,raw);v=unseal(self.token,receive_frame(stream));end=finite(self.clock());budget.remaining()
            if set(v)!={'version','session_uuid','epoch','nonce','request_id','sequence','issued_at','ttl','telemetry'} or type(v['version']) is not int or v['version']!=1 or type(v['sequence']) is not int or (v['session_uuid'],v['epoch'],v['nonce'],v['request_id'],v['sequence'])!=(self.spec.session_uuid,self.spec.epoch,nonce,rid,self.sequence):raise SecurityError('Export reply correlation rejected')
            issued,ttl=finite(v['issued_at']),finite(v['ttl'])
            if not now<=issued<=end<issued+ttl<=now+1 or type(v['telemetry']) is not str:raise SecurityError('Export reply expired')
            self.gate.authorize('connect_export');return peer,v['telemetry'].encode()
        except Exception:
            self.closed=True;self.connector.closed=True;self.gate.close();raise SecurityError('Export client blocked') from None
        finally:
            if stream is not None:stream.close()
            self.lock.release()


def export_listener(gate,server,backend):
    if type(gate) is not ExportChannelGate or gate.context.role!='px4':raise SecurityError('P export gate required')
    gate.authorize('listen_export')
    return Listener(gate.endpoint,gate.metadata,server,backend,clock=gate.clock)


def poll_export(client,receiver):
    """A single broker event loop owns this call and Agent dispatch; no threads."""
    nonce=str(new_uuid())
    try:
        peer,raw=client(nonce);receiver.accept(peer,raw,nonce)
    except Exception:
        receiver.close();raise SecurityError('Export pump stopped') from None
