"""Append-only, bounded durable reservations. No key-file access or key generation.

A claim is consumed BEFORE any transport may start. Orphan intent/record is
ambiguous and fails closed; recovery never deletes it to enable reuse. Checksums
protect accidental corruption, not same-owner tampering or snapshot rollback.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat
from threading import Lock
from .spec import SecurityError, canonical, integer, private_path, uuid4


MAX_RECORD = 2048
MAX_CLAIMS = 256


def fingerprint(value):
    if type(value) is bytes and len(value)==32:return value.hex()
    raise SecurityError('Full key fingerprint required')


class DurableClaims:
    """Private directory and descriptor ACL proof must already be provisioned.

    create=True initializes an EMPTY fixture/new approved ledger exactly once.
    A missing initialized ledger never silently resets. No chmod/chown or key IO.
    Every reserved session/epoch/key is permanently burned, including aborts.
    """
    def __init__(self,path,owner_uid,*,acl_checker=None,create=False,fault=None,capacity=MAX_CLAIMS):
        self.path=private_path(str(path)); self.owner=integer(owner_uid,1,2**31-1)
        self.acl_checker=acl_checker; self.fault=fault or (lambda _:None)
        self.capacity=integer(capacity,1,MAX_CLAIMS); self.closed=False; self._mutex=Lock(); self._issued={}
        if acl_checker is None:raise SecurityError('Descriptor ACL proof required')
        with self._locked() as directory:
            names=self._names(directory)
            if create:
                if names!={'lock'}:raise SecurityError('Ledger initialization collision')
                self._exclusive(directory,'header',canonical(dict(version=1,capacity=self.capacity)))
                os.fsync(directory)
            self._read_all(directory)

    def _metadata(self,fd,kind,mode):
        info=os.fstat(fd)
        if (info.st_uid!=self.owner or stat.S_IMODE(info.st_mode)!=mode
                or (kind=='dir' and not stat.S_ISDIR(info.st_mode))
                or (kind=='file' and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1))
                or self.acl_checker(fd) is not True):
            raise SecurityError('Ledger metadata/ACL rejected')
        return info

    def _directory(self):
        # Each component is opened without following symlinks. System ancestors
        # must be root or owner, not group/world writable. Fixture /private/tmp
        # is not accepted; persistence tests inject a private fixture FD and
        # test ancestor rejection separately without changing project modes.
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
        try:
            parts=PurePosixPath(self.path).parts[1:]
            for index,name in enumerate(parts):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                os.close(fd); fd=child
                info=os.fstat(fd)
                if index==len(parts)-1:self._metadata(fd,'dir',0o700)
                elif (info.st_uid not in (0,self.owner) or stat.S_IMODE(info.st_mode)&0o022
                      or self.acl_checker(fd) is not True):raise SecurityError('Unsafe ledger ancestor')
            return fd
        except Exception:
            os.close(fd); raise

    @contextmanager
    def _locked(self):
        directory=lock_fd=None
        acquired=False
        try:
            if self.closed:raise SecurityError('Ledger unavailable')
            if not self._mutex.acquire(blocking=False):raise SecurityError('Ledger busy')
            acquired=True
            directory=self._directory()
            lock_fd=os.open('lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,0o600,dir_fd=directory)
            self._metadata(lock_fd,'file',0o600)
            fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield directory
        except Exception:
            self.closed=True
            raise SecurityError('Durable ledger rejected') from None
        finally:
            if lock_fd is not None:os.close(lock_fd)
            if directory is not None:os.close(directory)
            if acquired:self._mutex.release()

    def _read(self,directory,name):
        fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=directory)
        try:
            before=self._metadata(fd,'file',0o600)
            if before.st_size>MAX_RECORD:raise SecurityError('Ledger record too large')
            data=os.read(fd,MAX_RECORD+1)
            after=os.fstat(fd)
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(
                    after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise SecurityError('Ledger record changed')
            if len(data)!=before.st_size:raise SecurityError('Truncated ledger record')
            return data
        finally:os.close(fd)

    def _exclusive(self,directory,name,data):
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
        try:
            self._metadata(fd,'file',0o600)
            view=memoryview(data)
            while view:
                written=os.write(fd,view)
                if written<=0:raise SecurityError('Incomplete ledger write')
                view=view[written:]
            os.fsync(fd)
        finally:os.close(fd)

    def _names(self,directory):
        names=set()
        with os.scandir(directory) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names)>self.capacity+2:raise SecurityError('Ledger entry budget exceeded')
        return names

    def _read_all(self,directory):
        names=self._names(directory)
        header_raw=self._read(directory,'header')
        header=json.loads(header_raw)
        if canonical(header)!=header_raw:raise SecurityError('Ledger header encoding rejected')
        if type(header.get('version')) is not int or type(header.get('capacity')) is not int or header!={'version':1,'capacity':self.capacity}:raise SecurityError('Ledger header mismatch')
        if len(names)>self.capacity+2:raise SecurityError('Ledger capacity/corruption')
        if any(name not in ('lock','header') and not (len(name)==64 and all(c in '0123456789abcdef' for c in name)) for name in names):
            raise SecurityError('Ambiguous ledger intent or unexpected entry')
        records=[]; sessions=set(); epochs=set(); keys=set()
        for name in names-{'lock','header'}:
            raw=self._read(directory,name)
            if hashlib.sha256(raw).hexdigest()!=name:raise SecurityError('Ledger checksum mismatch')
            value=json.loads(raw)
            if canonical(value)!=raw or set(value)!={'version','session_uuid','epoch','key_sha256','spec_sha256'} or type(value['version']) is not int or value['version']!=1:
                raise SecurityError('Ledger record schema rejected')
            uuid4(value['session_uuid']); uuid4(value['epoch'])
            for field in ('key_sha256','spec_sha256'):
                if type(value[field]) is not str or len(value[field])!=64 or any(c not in '0123456789abcdef' for c in value[field]):
                    raise SecurityError('Ledger digest invalid')
            if value['session_uuid'] in sessions or value['epoch'] in epochs or value['key_sha256'] in keys:
                raise SecurityError('Ledger duplicate claim')
            sessions.add(value['session_uuid']); epochs.add(value['epoch']); keys.add(value['key_sha256']); records.append(value)
        return records

    def reserve(self,spec,key_fingerprint):
        key=fingerprint(key_fingerprint)
        with self._locked() as directory:
            records=self._read_all(directory)
            if (len(records)>=self.capacity or any(r['session_uuid']==spec.session_uuid or r['epoch']==spec.epoch or r['key_sha256']==key for r in records)):
                raise SecurityError('Identity reuse or ledger full')
            data=canonical(dict(version=1,session_uuid=spec.session_uuid,epoch=spec.epoch,key_sha256=key,spec_sha256=spec.digest))
            name=hashlib.sha256(data).hexdigest()
            # Intent persisted first: crash anywhere before final commit leaves
            # a marker which blocks ALL use until independently reviewed.
            intent='intent-'+name
            self._exclusive(directory,intent,data); os.fsync(directory); self.fault('intent_synced')
            os.link(intent,name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
            os.fsync(directory); self.fault('record_linked')
            os.unlink(intent,dir_fd=directory)
            os.fsync(directory); self.fault('commit_synced')
            if self.closed:raise SecurityError('Concurrent ledger failure latched')
            lease=ClaimLease(spec,key)
            self._issued[id(lease)]=lease
            return lease


    def verify_lease(self,lease,spec,key_fingerprint):
        key=fingerprint(key_fingerprint)
        with self._locked() as directory:
            if (self._issued.get(id(lease)) is not lease or lease._failed
                    or lease._spec!=spec or lease._key!=key):
                raise SecurityError('Active original reservation handle required')
            rows=self._read_all(directory)
            expected=dict(version=1,session_uuid=spec.session_uuid,epoch=spec.epoch,key_sha256=key,spec_sha256=spec.digest)
            if sum(row==expected for row in rows)!=1:raise SecurityError('Durable reservation lost')
            return True


class ClaimLease:
    """One in-process activation after a durable reservation, never persisted."""
    def __init__(self,spec,key):
        self._spec=spec; self._key=key; self._session_used=False; self._key_used=False
        self._ready=False; self._failed=False;self._start_used=False; self._lock=Lock()

    def consume_start_receipt(self):
        with self._lock:
            if self._failed or self._start_used:raise SecurityError('Fresh start reservation required')
            self._start_used=True

    def revoke(self):
        with self._lock:
            self._ready=False;self._failed=True

    def confirm_px4(self, **evidence):
        with self._lock:
            try:
                if self._failed or self._session_used or self._key_used:
                    raise SecurityError('Lease cannot be reactivated')
                self._ready=False
                self._confirm_px4(**evidence)
            except Exception:
                self._ready=False; self._failed=True
                raise SecurityError('PX4 ledger agreement rejected') from None

    def _confirm_px4(self, *, peer, session_uuid, epoch, key_sha256, marker, parent, now):
        from .platform_macos import require_peer
        require_peer(peer,self._spec.px4_uid)
        from .access import validate_node
        self._spec.validate_time(now)
        if (session_uuid,epoch,key_sha256)!=(self._spec.session_uuid,self._spec.epoch,self._key):
            raise SecurityError('PX4/broker ledger mismatch')
        if parent.path!=self._spec.key_ledger_dir or marker.path!=parent.path+'/'+self._key:
            raise SecurityError('PX4 ledger path mismatch')
        validate_node(parent,uid=self._spec.px4_uid,kind='dir',mode=0o700)
        validate_node(marker,uid=self._spec.px4_uid,kind='file',mode=0o600)
        from .spec import finite
        if marker.size!=0 or marker.captured_at is None or not 0 <= finite(now)-finite(marker.captured_at)<=1:
            raise SecurityError('PX4 marker must be empty and freshly observed')
        # Zero-length marker contents are PX4-specific; actual reader must prove
        # freshness/size via its trusted collection boundary, not user JSON.
        self._ready=True

    def claim(self,value):
        with self._lock:
            if self._failed or not self._ready:raise SecurityError('PX4 ledger agreement not established')
            if isinstance(value,bytes):
                if fingerprint(value)!=self._key or self._key_used:raise SecurityError('Key lease rejected')
                self._key_used=True
            else:
                if value!=self._spec or self._session_used:raise SecurityError('Session lease rejected')
                self._session_used=True
