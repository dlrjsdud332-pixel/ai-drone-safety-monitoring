"""Endpoint/connector/listener/datagram lifecycle over trusted injected backend.

Default backend refuses every create. There is no runtime activation CLI.
"""
from dataclasses import dataclass, replace
from threading import BoundedSemaphore, Lock
import time
from .access import ACE, validate_private_chain
from .platform_macos import PeerCredentials, require_peer
from .spec import SecurityError, finite, integer, private_path


class Deadline:
    def __init__(self, seconds=2.0, clock=time.monotonic):
        self._clock = clock
        seconds = finite(seconds)
        if not 0 < seconds <= 5:raise SecurityError('Deadline bound rejected')
        self._end = finite(clock())+seconds

    def remaining(self):
        value = self._end-finite(self._clock())
        if value <= 0:raise SecurityError('Transaction deadline exceeded')
        return value


class DeadlineStream:
    """A single absolute budget covers connect, credentials, frame and dispatch."""
    def __init__(self, stream, deadline):
        self._stream, self._deadline = stream, deadline
        self.family, self.type = getattr(stream,'family',None), getattr(stream,'type',None)

    def _io(self, method, *args):
        self._stream.settimeout(self._deadline.remaining())
        result = getattr(self._stream,method)(*args)
        self._deadline.remaining()
        return result

    def recv(self, size):return self._io('recv',size)
    def sendall(self, raw):return self._io('sendall',raw)
    def settimeout(self, value):self._stream.settimeout(min(value,self._deadline.remaining()))
    def fileno(self):return self._stream.fileno()
    def getpeereid(self):
        self._deadline.remaining()
        peer=PeerCredentials.from_connected_socket(self._stream)
        self._deadline.remaining()
        return peer.uid,peer.gid
    def close(self):self._stream.close()


@dataclass(frozen=True)
class Endpoint:
    path: str
    broker_uid: int
    agent_uid: int

    def __post_init__(self):
        private_path(self.path)
        if len(self.path.encode())>=104 or not self.path.endswith('/api.sock'):
            raise SecurityError('API endpoint path rejected')
        integer(self.broker_uid,1,2**31-1); integer(self.agent_uid,1,2**31-1)
        if self.agent_uid == self.broker_uid:raise SecurityError('Independent endpoint identities required')

    def verify_parent(self,reader):
        from .access import ancestors,validate_node
        nodes=[]
        parent=self.path.rsplit('/',1)[0]
        for path in ancestors(self.path):
            node=reader(path)
            if node.path!=path:raise SecurityError('Endpoint parent evidence mismatch')
            if path==parent:
                validate_node(node,uid=self.broker_uid,kind='dir',mode=0o700,
                    acl=(ACE(self.agent_uid,frozenset({'search'})),))
            else:
                if node.uid not in (0,self.broker_uid) or node.kind!='dir' or node.mode&0o022 or node.acl:
                    raise SecurityError('Unsafe endpoint ancestor')
                validate_node(node,uid=node.uid,kind='dir',mode=node.mode)
            nodes.append(node)
        return tuple(nodes)

    def verify(self, reader):
        acl=(ACE(self.agent_uid,frozenset({'read','write'})),)
        return validate_private_chain(self.path,reader,self.broker_uid,
            leaf_kind='socket',leaf_mode=0o600,leaf_acl=acl,api_agent=self.agent_uid)


class DisabledBackend:
    def stream(self):raise SecurityError('Live socket execution not authorized')
    def listener(self):raise SecurityError('Live socket execution not authorized')
    def datagram(self):raise SecurityError('Live socket execution not authorized')


def _close(obj):
    if obj is not None:
        try:obj.close()
        except Exception:pass


class Connector:
    def __init__(self, endpoint, reader, backend=None, credentials=PeerCredentials.from_connected_socket, clock=time.monotonic):
        self.endpoint,self.reader = endpoint,reader
        self.backend = backend or DisabledBackend()
        self.credentials,self.clock = credentials,clock
        self.closed = False

    def __call__(self,deadline=None):
        stream = None
        try:
            if self.closed:raise SecurityError('Connector closed')
            if deadline is not None and type(deadline) is not Deadline:raise SecurityError('Typed transaction budget required')
            budget = deadline or Deadline(clock=self.clock)
            before = self.endpoint.verify(self.reader)
            budget.remaining()
            stream = self.backend.stream()
            stream.settimeout(budget.remaining())
            stream.connect(self.endpoint.path)
            wrapped = DeadlineStream(stream,budget)
            require_peer(self.credentials(wrapped),self.endpoint.broker_uid)
            after=self.endpoint.verify(self.reader)
            stable=lambda nodes:tuple(replace(node,captured_at=None) for node in nodes)
            if stable(after)!=stable(before):raise SecurityError('Endpoint replaced')
            budget.remaining()
            return wrapped
        except Exception:
            self.closed = True; _close(stream)
            raise SecurityError('Endpoint connection rejected') from None


class Listener:
    """No accept loop or background threads; one explicit poll per call.

    Backend must create an owned mode/ACL-correct path atomically; no existing
    path is unlinked. Socket/node cleanup is a separate identity-checked plan.
    """
    def __init__(self, endpoint, reader, server, backend=None, credentials=PeerCredentials.from_connected_socket,
                 clock=time.monotonic, maximum=4):
        self.endpoint,self.reader,self.server = endpoint,reader,server
        self.backend = backend or DisabledBackend()
        self.credentials,self.clock = credentials,clock
        self._slots = BoundedSemaphore(integer(maximum,1,8))
        self._lock = Lock(); self._listener = None; self._active = set(); self.closed = False
        self._recent = []

    def open(self):
        listener = None
        try:
            with self._lock:
                if self.closed or self._listener is not None:raise SecurityError('Listener state rejected')
                # Trusted backend must refuse collisions and safely apply endpoint ACL.
                # Validate parents before any creation; absence is required at backend.
                from .access import ancestors, validate_node, ACE
                for name in ancestors(self.endpoint.path):
                    node = self.reader(name)
                    if node.path!=name:raise SecurityError('Ancestor path mismatch')
                    if name == self.endpoint.path.rsplit('/',1)[0]:
                        validate_node(node,uid=self.endpoint.broker_uid,kind='dir',mode=0o700,
                            acl=(ACE(self.endpoint.agent_uid,frozenset({'search'})),))
                    elif node.uid not in (0,self.endpoint.broker_uid) or node.kind!='dir' or node.mode & 0o022 or node.acl:
                        raise SecurityError('Unsafe listener ancestor')
                listener = self.backend.listener()
                listener.settimeout(0.1)
                listener.bind_private(self.endpoint)  # O_EXCL-like refusal; no unlink fallback.
                self.endpoint.verify(self.reader)
                listener.listen(4)
                self._listener = listener
        except Exception:
            _close(listener); self.close()
            raise SecurityError('Listener initialization rejected') from None

    def poll_once(self):
        if not self._slots.acquire(blocking=False):raise SecurityError('Connection capacity exceeded')
        stream = None
        try:
            with self._lock:
                if self.closed or self._listener is None:raise SecurityError('Listener unavailable')
                now=finite(self.clock()); self._recent=[t for t in self._recent if now-t<1]
                if len(self._recent)>=16:raise SecurityError('Connection rate exceeded')
                self._recent.append(now)
                listener=self._listener
            budget=Deadline(clock=self.clock)
            stream,_ = listener.accept()
            wrapped=DeadlineStream(stream,budget)
            require_peer(self.credentials(wrapped),self.endpoint.agent_uid)
            self.endpoint.verify(self.reader); budget.remaining()
            with self._lock:
                if self.closed:raise SecurityError('Shutdown in progress')
                self._active.add(wrapped)
            self.server.handle_connected(wrapped)
            budget.remaining()
        except Exception:
            # No packet, key, path or external exception is logged.
            raise SecurityError('Listener transaction rejected') from None
        finally:
            with self._lock:
                if 'wrapped' in locals():self._active.discard(wrapped)
            _close(stream); self._slots.release()

    def close(self):
        with self._lock:
            self.closed=True; listener=self._listener; self._listener=None
            streams=tuple(self._active); self._active.clear()
        _close(listener)
        for stream in streams:_close(stream)
        # Do not block waiting for a dispatch lock or join a peer-controlled thread.
        # Active operations have absolute deadlines; no new accept is permitted.


class BoundDatagrams:
    """Create only two explicitly owned loopback channels via injected backend."""
    def __init__(self,spec,ownership_check,backend=None):
        self.spec,self.ownership_check=spec,ownership_check
        self.backend=backend or DisabledBackend(); self._channels=[]; self.closed=False

    def open(self):
        try:
            if self.closed or self._channels:raise SecurityError('Datagram state rejected')
            for link in self.spec.links:
                channel=self.backend.datagram(); self._channels.append(channel)
                channel.settimeout(2.0); channel.bind((link.address,link.client_port))
                if channel.getsockname()!=(link.address,link.client_port):raise SecurityError('Local bind mismatch')
            if self.ownership_check(self.spec,tuple(self._channels)) is not True:
                raise SecurityError('Fresh PID ownership proof required')
            return tuple(self._channels)
        except Exception:
            self.close(); raise SecurityError('Datagram preparation rejected') from None

    def close(self):
        self.closed=True
        for channel in self._channels:_close(channel)
        self._channels=[]


class LiveSocketBackend:
    """Dormant OS binding implementation; authorization provider NOT supplied.

    Trusted future service must independently attest approval, role, deployment,
    endpoint ACL and durable leases on EVERY creation. A request body or CLI flag
    is not an authorization provider. Current runner never instantiates this with
    an authorizer. Default is denial before even importing/creating a socket.
    """
    def __init__(self, authorizer=None, endpoint_acl_installer=None):
        self._authorizer=authorizer; self._acl_installer=endpoint_acl_installer

    def _require(self, operation):
        from .activation import TrustedActivationProvider
        from .agent_activation import AgentActivationProvider
        if type(self._authorizer) not in (TrustedActivationProvider,AgentActivationProvider):
            raise SecurityError('Trusted activation provider required')
        import os
        expected=self._authorizer.spec.broker_uid if type(self._authorizer) is TrustedActivationProvider else self._authorizer.view.agent_uid
        if os.geteuid()!=expected:
            self._authorizer.close()
            raise SecurityError('Active OS role required before socket creation')
        permit=self._authorizer.authorize(operation)
        self._authorizer.consume_socket_permit(permit,operation,mode='live')

    def _create(self,operation,kind):
        raw=None
        try:
            self._require(operation)
            if kind=='listener' and self._acl_installer is None:
                raise SecurityError('Approved ACL installer unavailable')
            import socket
            raw=socket.socket(socket.AF_INET if kind=='datagram' else socket.AF_UNIX,
                              socket.SOCK_DGRAM if kind=='datagram' else socket.SOCK_STREAM)
            return _PrivateListener(raw,self._acl_installer) if kind=='listener' else raw
        except Exception:
            _close(raw)
            from .activation import TrustedActivationProvider
            from .agent_activation import AgentActivationProvider
            if type(self._authorizer) in (TrustedActivationProvider,AgentActivationProvider):self._authorizer.close()
            raise SecurityError('Live socket allocation blocked') from None

    def stream(self):return self._create('connect_ipc','stream')
    def datagram(self):return self._create('bind_loopback_udp','datagram')
    def listener(self):return self._create('listen_ipc','listener')


class _PrivateListener:
    def __init__(self,raw,acl_installer):
        self._raw,self._acl_installer=raw,acl_installer

    def settimeout(self,value):self._raw.settimeout(value)
    def listen(self,value):self._raw.listen(value)
    def accept(self):return self._raw.accept()
    def close(self):self._raw.close()

    def bind_private(self,endpoint):
        import os
        # Caller already verified private parents. bind refuses existing paths;
        # never unlink one, and do not start listening before ACL proof passes.
        if os.geteuid()!=endpoint.broker_uid:raise SecurityError('Listener owner rejected')
        try:
            os.lstat(endpoint.path)
        except FileNotFoundError:pass
        else:raise SecurityError('Existing endpoint collision')
        self._raw.bind(endpoint.path)
        if self._acl_installer(endpoint) is not True:
            raise SecurityError('Endpoint ACL installation not established')
