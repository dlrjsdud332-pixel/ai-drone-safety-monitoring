"""Bounded IPC adapters over injected, already-connected streams.

No socket creation, listener binding, connect loop, service entrypoint or key reads.
Connection factories/listeners are a future trusted deployment boundary.
"""
import struct
from threading import RLock
from .spec import SecurityError, strict_json, finite
from .policy import unseal
from .client import ObservationClient
from .platform_macos import PeerCredentials, require_peer

LIMIT = 4096
IO_TIMEOUT = 2.0


class MemorySessionClaims:
    """Offline process-local restart guard; durable deployment backend required."""
    def __init__(self):
        self._sessions = set()
        self._epochs = set()
        self._lock = RLock()

    def claim(self, spec):
        with self._lock:
            if spec.session_uuid in self._sessions or spec.epoch in self._epochs:
                raise SecurityError('Retired session identity rejected')
            self._sessions.add(spec.session_uuid)
            self._epochs.add(spec.epoch)


_PROCESS_SESSION_CLAIMS = MemorySessionClaims()


def _read(stream, count):
    result = bytearray()
    while len(result) < count:
        chunk = stream.recv(count-len(result))
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > count-len(result):
            raise SecurityError('IPC frame unavailable')
        result.extend(chunk)
    return bytes(result)


def receive_frame(stream):
    size = struct.unpack('!I', _read(stream, 4))[0]
    if not 0 < size <= LIMIT:
        raise SecurityError('IPC frame bound rejected')
    raw = _read(stream, size)
    strict_json(raw)
    return raw


def send_frame(stream, raw):
    strict_json(raw)
    if not 0 < len(raw) <= LIMIT:
        raise SecurityError('IPC frame bound rejected')
    stream.sendall(struct.pack('!I', len(raw))+raw)


class IPCServerTransport:
    """One bounded transaction per supplied accepted stream; policy is shared.

    Invalid peers/requests close that connection. Internal IO/dispatch failure
    latches this server offline. Recovery requires a new broker/session/epoch.
    """
    def __init__(self, broker, credentials=PeerCredentials.from_connected_socket,
                 session_claims=_PROCESS_SESSION_CLAIMS):
        session_claims.claim(broker.spec)
        self._broker = broker
        self._credentials = credentials
        self._closed = False
        self._lock = RLock()

    def close(self):
        with self._lock:
            self._closed = True
            self._broker.telemetry.reset(self._broker.spec.session_uuid, self._broker.spec.epoch)

    def invalidate(self):
        self._closed=True
        self._broker.telemetry.reset(self._broker.spec.session_uuid,self._broker.spec.epoch)

    def handle_connected(self, stream):
        # Reject queued dispatch instead of waiting behind a peer-controlled IO.
        acquired = self._lock.acquire(blocking=False)
        try:
            if not acquired or self._closed:
                raise SecurityError('IPC server unavailable')
            stream.settimeout(IO_TIMEOUT)
            peer = self._credentials(stream)
            require_peer(peer, self._broker.spec.agent_uid)
            request = receive_frame(stream)
            response = self._broker.dispatch(peer, request)
            if self._closed:raise SecurityError('Server stopped before response')
            send_frame(stream, response)
        except SecurityError:
            raise SecurityError('IPC request rejected') from None
        except Exception:
            if acquired:self.close()
            raise SecurityError('IPC server unavailable') from None
        finally:
            try:
                stream.close()
            except Exception:
                if acquired:self.close()
            if acquired:self._lock.release()


class IPCClientTransport:
    """Callable compatible with ObservationClient.exchange, no private manifest.

    Factory must enforce connect/read/write deadlines and endpoint metadata.
    It is injected and never called by import or construction. Any failure
    latches transport closed; automatic reconnect/legacy fallback is forbidden.
    """
    def __init__(self, spec, api_token, connection_factory,
                 credentials=PeerCredentials.from_connected_socket, clock=None, response_schema='base'):
        if type(response_schema) is not str or response_schema not in ('base','PX4_TELEMETRY_V1','PX4_NATIVE_TELEMETRY_V1'):raise SecurityError('Explicit IPC response schema required')
        self._response_schema=response_schema
        self._uid = spec.broker_uid
        self._spec = spec
        self._token = api_token
        if clock is None:
            import time
            clock = time.time
        self._clock = clock
        self._factory = connection_factory
        self._credentials = credentials
        self._closed = False
        self._lock = RLock()

    def close(self):
        self._closed = True

    def __call__(self, request):
        with self._lock:
            stream = None
            try:
                if self._closed:
                    raise SecurityError('IPC client unavailable')
                body = unseal(self._token, request)
                self._spec.validate_time(self._clock())
                stream = self._factory()
                stream.settimeout(IO_TIMEOUT)
                peer = self._credentials(stream)
                require_peer(peer, self._uid)  # Before transmitting capability.
                send_frame(stream, request)
                response = receive_frame(stream)
                value = unseal(self._token, response)
                if set(value) != {'version','session_uuid','epoch','sequence','request_id','issued_at','ttl','operation','data'}:
                    raise SecurityError('Response schema rejected')
                for field in ('version','session_uuid','epoch','sequence','request_id','operation'):
                    if type(value[field]) is not type(body.get(field)) or value[field] != body.get(field):
                        raise SecurityError('Response correlation rejected')
                now = finite(self._clock())
                self._spec.validate_time(now)
                issued, ttl = finite(value['issued_at']), finite(value['ttl'])
                if not 0 < ttl <= 5 or not issued <= now < issued+ttl:
                    raise SecurityError('Response freshness rejected')
                if self._response_schema=='base':
                    validator = ObservationClient(self._spec, self._token, None, self._clock)
                elif self._response_schema=='PX4_TELEMETRY_V1':
                    from .telemetry_feed import TelemetryClient
                    validator=TelemetryClient(self._spec,self._token,None,self._clock)
                else:
                    from .telemetry_exporter import NativeTelemetryClient
                    validator=NativeTelemetryClient(self._spec,self._token,None,self._clock)
                validator._validate_data(body['operation'], value['data'])
                return peer, response
            except Exception:
                self.close()
                raise SecurityError('IPC exchange unavailable') from None
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        self.close()
