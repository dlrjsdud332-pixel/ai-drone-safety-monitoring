"""Strict snapshot/ACL policy. Evidence alone never grants runtime authority."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import os
import stat
from .spec import SecurityError, integer, private_path


@dataclass(frozen=True)
class ACE:
    uid: int
    permissions: frozenset
    inherited: bool = False
    allow: bool = True


@dataclass(frozen=True)
class Node:
    path: str
    uid: int
    mode: int
    kind: str
    inode: int
    device: int
    nlink: int = 1
    acl: tuple = ()
    size: int = 0
    captured_at: float = None


def validate_node(node, *, uid, kind, mode, acl=()):
    if (not isinstance(node, Node) or type(node.uid) is not int or node.uid != uid
            or node.kind != kind or type(node.mode) is not int or node.mode != mode
            or type(node.inode) is not int or node.inode <= 0
            or type(node.device) is not int or node.device < 0
            or type(node.nlink) is not int or node.nlink < 1
            or (kind in ('file','socket') and node.nlink != 1) or type(node.acl) is not tuple
            or node.acl != acl):
        raise SecurityError('Metadata/ACL policy rejected')
    private_path(node.path)
    for entry in node.acl:
        if (not isinstance(entry, ACE) or type(entry.uid) is not int or entry.uid <= 0
                or entry.inherited is not False or entry.allow is not True
                or type(entry.permissions) is not frozenset):
            raise SecurityError('ACL policy rejected')
    return node


class MetadataReader:
    """Read-only lstat adapter. ACL parser/proof is mandatory, no empty default.

    acl_reader(path, before_stat) must use a trusted macOS ACL implementation,
    resolve UUID principals, include inherited/deny entries and reject unknown
    rights. Tests inject fixture ACLs. No actual ACL parser is activated here.
    """
    def __init__(self, acl_reader=None, clock=None):
        import time
        self._clock=clock or time.time
        self._acl_reader = acl_reader

    def __call__(self, path):
        private_path(path)
        if self._acl_reader is None:
            raise SecurityError('Authoritative ACL reader unavailable')
        before = os.lstat(path)
        acl = self._acl_reader(path, before)
        after = os.lstat(path)
        if (before.st_dev,before.st_ino,before.st_mode,before.st_uid,before.st_ctime_ns) != (
                after.st_dev,after.st_ino,after.st_mode,after.st_uid,after.st_ctime_ns):
            raise SecurityError('Metadata changed during collection')
        kind = ('dir' if stat.S_ISDIR(before.st_mode) else 'file' if stat.S_ISREG(before.st_mode)
                else 'socket' if stat.S_ISSOCK(before.st_mode) else 'unsafe')
        return Node(path,before.st_uid,stat.S_IMODE(before.st_mode),kind,
                    before.st_ino,before.st_dev,before.st_nlink,acl,before.st_size,self._clock())


def ancestors(path):
    private_path(path)
    # Root itself is trusted by OS; every named path component is checked.
    return tuple(str(p) for p in reversed(PurePosixPath(path).parents) if str(p) != '/')


def validate_private_chain(path, reader, owner, *, leaf_kind, leaf_mode, leaf_acl=(), api_agent=None):
    integer(owner,1,2**31-1)
    parent = str(PurePosixPath(path).parent)
    result = []
    for name in ancestors(path):
        node = reader(name)
        if name == parent:
            acl = () if api_agent is None else (ACE(api_agent,frozenset({'search'})),)
            validate_node(node,uid=owner,kind='dir',mode=0o700,acl=acl)
        else:
            if node.uid not in (0,owner) or node.mode & 0o022 or node.kind != 'dir' or node.acl:
                raise SecurityError('Unsafe ancestor')
            validate_node(node,uid=node.uid,kind='dir',mode=node.mode)
        if node.path != name:raise SecurityError('Path evidence mismatch')
        result.append(node)
    leaf = reader(path)
    if leaf.path != path:raise SecurityError('Path evidence mismatch')
    validate_node(leaf,uid=owner,kind=leaf_kind,mode=leaf_mode,acl=leaf_acl)
    return tuple(result)+(leaf,)


def validate_udp_ownership(spec, expected, observed, records, *, role, captured_at, now, max_age=1.0):
    from .session import validate_observed_identity, OwnedIdentity
    from .spec import finite
    age = finite(now)-finite(captured_at)
    if not 0 <= age <= max_age:raise SecurityError('Socket evidence expired')
    if role == 'px4':
        validate_observed_identity(spec,expected,observed)
        ports = {link.receiver_port for link in spec.links}
        uid = spec.px4_uid
    elif role == 'broker':
        if (not isinstance(expected,OwnedIdentity) or expected != observed or expected.pid <= 0
                or expected.uid != spec.broker_uid or expected.session_uuid != spec.session_uuid
                or not isinstance(expected.birth,str) or not expected.birth
                or len(expected.binary_sha256)!=64):
            raise SecurityError('Broker process identity rejected')
        ports = {link.client_port for link in spec.links}; uid = spec.broker_uid
    else:raise SecurityError('Unknown socket owner role')
    if (len(records)!=2 or any(set(row)!={'pid','uid','address','port','protocol','family'}
            or row['pid']!=expected.pid or row['uid']!=uid or row['address']!='127.0.0.1'
            or row['protocol']!='UDP' or row['family']!='IPv4' for row in records)
            or {row['port'] for row in records} != ports):
        raise SecurityError('Exact owned loopback sockets required')
    return dict(status='PASS',scope='FRESH_SUPPLIED_EVIDENCE_ONLY')
