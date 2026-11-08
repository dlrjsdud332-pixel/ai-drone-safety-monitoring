"""Immutable, explicit session contracts; no legacy target/port fallback."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
from uuid import UUID


class SecurityError(ValueError):
    pass


def integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise SecurityError('Invalid integer field')
    return value


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise SecurityError('Non-finite numeric field')
    return float(value)


def uuid4(value):
    if not isinstance(value, str):
        raise SecurityError('UUID must be a string')
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise SecurityError('Invalid UUID') from exc
    if parsed.version != 4 or str(parsed) != value:
        raise SecurityError('Canonical UUID4 required')
    return value


def private_path(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
        raise SecurityError('Explicit absolute private path required')
    p = PurePosixPath(value)
    if '..' in p.parts or str(p) != value or value == '/':
        raise SecurityError('Non-canonical private path')
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise SecurityError('Invalid JSON value') from exc


def strict_json(raw, limit=4096):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise SecurityError('Invalid or oversized envelope')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SecurityError('Duplicate JSON field')
            result[key] = value
        return result
    def bad(_):
        raise SecurityError('Non-finite JSON constant')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SecurityError('Invalid envelope JSON') from exc
    if not isinstance(value, dict):
        raise SecurityError('Object envelope required')
    return value


@dataclass(frozen=True)
class LinkSpec:
    receiver_port: int
    client_port: int
    link_id: int
    address: str = '127.0.0.1'

    def __post_init__(self):
        integer(self.receiver_port, 1024, 65535)
        integer(self.client_port, 1024, 65535)
        integer(self.link_id, 0, 255)
        if self.address != '127.0.0.1':
            raise SecurityError('Only IPv4 loopback is allowed')


@dataclass(frozen=True)
class SecureSessionSpec:
    session_uuid: str
    epoch: str
    instance_id: int
    vehicle_system: int
    vehicle_component: int
    controller_system: int
    controller_component: int
    agent_uid: int
    broker_uid: int
    px4_uid: int
    issued_at: float
    expires_at: float
    runtime_dir: str
    key_ledger_dir: str
    binary_sha256: str
    policy_sha256: str
    links: tuple
    schema_version: int = 1
    backend: str = 'native'
    profile: str = 'receiver_only_strict'
    capability: str = 'monitor_only'

    def __post_init__(self):
        uuid4(self.session_uuid); uuid4(self.epoch)
        integer(self.instance_id, 1, 255)
        if self.instance_id in (1, 7, 218, 219):
            raise SecurityError('Existing or historical receiver instance is forbidden')
        for name in ('vehicle_system', 'vehicle_component', 'controller_system', 'controller_component'):
            integer(getattr(self, name), 1, 255)
        if self.vehicle_system in (1, 8, 219, 220) or self.vehicle_system == self.controller_system:
            raise SecurityError('Existing vehicle identity or identity collision')
        for uid in (self.agent_uid, self.broker_uid, self.px4_uid):
            integer(uid, 1, 2**31-1)
        if len({self.agent_uid, self.broker_uid, self.px4_uid}) != 3:
            raise SecurityError('Three distinct non-root identities required')
        start, end = finite(self.issued_at), finite(self.expires_at)
        if start < 0 or not 0 < end-start <= 3600:
            raise SecurityError('Invalid session lifetime')
        private_path(self.runtime_dir); private_path(self.key_ledger_dir)
        if PurePosixPath(self.key_ledger_dir).is_relative_to(PurePosixPath(self.runtime_dir)):
            raise SecurityError('Persistent key ledger must survive session-runtime cleanup')
        if len((self.runtime_dir+'/p.sock').encode()) >= 104:
            raise SecurityError('Unix socket path would be truncated on macOS')
        for digest in (self.binary_sha256, self.policy_sha256):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise SecurityError('Explicit SHA256 required')
        if (self.schema_version != 1 or type(self.schema_version) is not int or self.backend != 'native'
                or self.profile != 'receiver_only_strict' or self.capability != 'monitor_only'):
            raise SecurityError('Unsupported profile or capability')
        if not isinstance(self.links, tuple) or len(self.links) != 2 or not all(isinstance(l, LinkSpec) for l in self.links):
            raise SecurityError('Exactly two immutable links required')
        ports = [p for l in self.links for p in (l.receiver_port, l.client_port)]
        forbidden = {10317,10318,13030,13280,14030,14280,14540,14550,14560,14561,14580,14641,14681,18570,19541,19581,19641,19681}
        if len(set(ports)) != 4 or set(ports) & forbidden or len({l.link_id for l in self.links}) != 2:
            raise SecurityError('Port/link identity collision or existing endpoint')

    def validate_time(self, now):
        now = finite(now)
        if not self.issued_at <= now < self.expires_at:
            raise SecurityError('Session not active')

    def public(self):
        value = asdict(self)
        for key in ('runtime_dir', 'key_ledger_dir', 'px4_uid'):
            value.pop(key)
        return value

    def protected(self):
        return asdict(self)

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.protected())).hexdigest()

    @classmethod
    def from_dict(cls, value):
        from dataclasses import fields
        required = {f.name for f in fields(cls)}
        if not isinstance(value, dict) or set(value) != required:
            raise SecurityError('Manifest fields must match the protected schema exactly')
        value = dict(value)
        try:
            value['links'] = tuple(LinkSpec(**v) for v in value['links'])
            return cls(**value)
        except (TypeError, KeyError) as exc:
            raise SecurityError('Invalid manifest structure') from exc
