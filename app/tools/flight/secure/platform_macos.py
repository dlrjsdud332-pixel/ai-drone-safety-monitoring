"""Credential/metadata checks on supplied objects only; never connects sockets."""
from dataclasses import dataclass
import os
import stat
from .spec import SecurityError, integer, private_path


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    gid: int
    def __post_init__(self):
        integer(self.uid, 0, 2**31-1); integer(self.gid, 0, 2**31-1)

    @classmethod
    def from_connected_socket(cls, connection):
        # Only a future trusted server may supply its already accepted socket.
        # Never use UID/GID fields from a request body.
        if hasattr(connection, 'getpeereid'):
            uid, gid = connection.getpeereid()
            return cls(uid, gid)
        # CPython macOS sockets do not consistently expose getpeereid as a
        # method. Query libc on an already accepted AF_UNIX descriptor only.
        import ctypes
        import socket
        import sys
        try:
            fd=connection.fileno()
            if (sys.platform!='darwin' or type(fd) is not int or fd<0
                    or connection.family!=socket.AF_UNIX or connection.type!=socket.SOCK_STREAM):
                raise SecurityError('OS peer credentials unavailable')
            library=ctypes.CDLL(None,use_errno=True)
            function=library.getpeereid
            function.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
            function.restype=ctypes.c_int
            uid=ctypes.c_uint();gid=ctypes.c_uint()
            if function(fd,ctypes.byref(uid),ctypes.byref(gid))!=0:
                raise SecurityError('OS peer credential query failed')
            return cls(uid.value,gid.value)
        except (OSError,AttributeError,TypeError,ValueError) as exc:
            raise SecurityError('OS peer credentials unavailable') from exc


def require_peer(peer, expected_uid):
    if not isinstance(peer, PeerCredentials) or peer.uid != expected_uid:
        raise SecurityError('OS peer identity rejected')


def checked_file(path, owner_uid, mode=0o600, maximum=16384):
    """Read a protected NON-KEY manifest via openat; reject unsafe ancestors.

    Root-owned ancestors must be non-writable by others. ACL deployment proof
    is deliberately separate and mandatory in load_manifest().
    """
    private_path(str(path))
    parts = str(path).split('/')[1:]
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, owner_uid) or stat.S_IMODE(info.st_mode) & 0o022:
                raise SecurityError('Unsafe manifest ancestor')
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != mode or info.st_size > maximum:
                raise SecurityError('Unsafe protected manifest')
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(file_fd, min(4096, maximum+1-len(data)))
                if not chunk:break
                data.extend(chunk)
            after = os.fstat(file_fd)
            if len(data) > maximum or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SecurityError('Manifest changed or exceeded bound')
            return bytes(data)
        finally:os.close(file_fd)
    except OSError as exc:
        raise SecurityError('Protected manifest unavailable') from exc
    finally:os.close(fd)
