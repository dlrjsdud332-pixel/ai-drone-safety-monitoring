"""Authenticated bounded envelopes. Shared-secret API token is NOT a MAVLink key."""
import hashlib
import hmac
from threading import RLock
from collections import deque
from .spec import SecurityError, canonical, finite, integer, strict_json, uuid4
from .platform_macos import require_peer

OPERATIONS = {'observe', 'status', 'probe_plan'}
FIELDS = {'version','session_uuid','epoch','sequence','request_id','issued_at','ttl','operation','proof'}


def proof(token, body):
    if not isinstance(token, bytes) or len(token) < 32:
        raise SecurityError('API capability must be at least 32 bytes')
    return hmac.new(token, canonical(body), hashlib.sha256).hexdigest()


def seal(token, body):
    if 'proof' in body:raise SecurityError('Ambiguous authentication field')
    return canonical(dict(body, proof=proof(token, body)))


def unseal(token, raw):
    value = strict_json(raw)
    signature = value.pop('proof', None)
    if not isinstance(signature, str) or not hmac.compare_digest(proof(token, value), signature):
        raise SecurityError('API authentication rejected')
    return value


class RequestPolicy:
    def __init__(self, spec, token, clock):
        proof(token, {})
        self.spec=spec; self._token=token; self.clock=clock
        self._sequence=0; self._ids=set(); self._count=0; self._recent=deque(); self._lock=RLock()

    def accept(self, peer, raw):
        with self._lock:
            require_peer(peer, self.spec.agent_uid)
            value = unseal(self._token, raw)
            if set(value) != FIELDS-{'proof'} or type(value['version']) is not int or value['version'] != 1:
                raise SecurityError('Invalid request schema')
            if value['session_uuid'] != self.spec.session_uuid or value['epoch'] != self.spec.epoch:
                raise SecurityError('Session or epoch mismatch')
            now=finite(self.clock()); self.spec.validate_time(now)
            issued=finite(value['issued_at']); ttl=finite(value['ttl'])
            if not 0 < ttl <= 5 or not issued <= now < issued+ttl or issued < self.spec.issued_at:
                raise SecurityError('Request expired or not yet valid')
            seq=integer(value['sequence'],1,2**53-1); uuid4(value['request_id'])
            if seq <= self._sequence or value['request_id'] in self._ids:
                raise SecurityError('API replay rejected')
            if not isinstance(value['operation'], str) or value['operation'] not in OPERATIONS:
                raise SecurityError('Operation is not permitted')
            if self._count >= 128:
                raise SecurityError('Session request budget exhausted')
            while self._recent and self._recent[0] <= now-1.0:self._recent.popleft()
            if len(self._recent)>=16:
                raise SecurityError('Per-session request rate exceeded')
            self._recent.append(now)
            self._sequence=seq; self._ids.add(value['request_id']); self._count+=1
            return value

    def respond(self, value):
        return seal(self._token, value)
