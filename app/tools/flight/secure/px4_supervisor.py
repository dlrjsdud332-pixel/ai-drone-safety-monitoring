"""Approval-bound owned PX4 lifecycle CONTRACT, never supplies an OS launcher.

Root deployment binds a reviewed process manager after explicit execution
approval. Tests supply memory handles. No Popen/kill/chmod/key creation exists.
"""
from dataclasses import asdict
import hashlib
import os
from uuid import uuid4
from .access import ancestors,validate_node,validate_private_chain
from .agent_activation import RootPublicDocument
from .identity_checks import RuntimeIdentity,CodeClosureChecker
from .platform_macos import checked_file,require_peer
from .policy import seal,unseal
from .service_entry import ServiceContext
from .session import startup_plan,validate_observed_identity
from .spec import SecurityError,canonical,finite,integer

APP='/Library/Application Support/AIDroneSecure'


def reservation_receipt(spec,ledger,lease,fingerprint,identity,token,challenge,now,*,mode):
    from .spec import uuid4 as validate_uuid
    validate_uuid(challenge);spec.validate_time(now)
    if mode not in ('fixture','live') or type(identity) is not RuntimeIdentity or identity.uid!=spec.broker_uid or identity.session_uuid!=spec.session_uuid:
        raise SecurityError('Original Broker reservation identity required')
    if mode=='live' and os.geteuid()!=spec.broker_uid:raise SecurityError('Active Broker UID required')
    ledger.verify_lease(lease,spec,fingerprint)
    lease.consume_start_receipt()
    return seal(token,dict(version=1,mode=mode,session_uuid=spec.session_uuid,epoch=spec.epoch,spec_sha256=spec.digest,
        key_sha256=fingerprint.hex(),identity=asdict(identity),challenge=challenge,issued_at=now,ttl=1.))


class DisabledOwnedLauncher:
    mode='disabled'
    def launch(self,*args):raise SecurityError('Actual PX4 execution not approved or bound')


class OwnedPX4Supervisor:
    """Fresh reservation -> P gate -> owned child -> trusted collector -> Broker.

    The explicit Root start document is separate from socket/trial approval.
    Stop never selects an instance/PID from a CLI; it verifies the same handle.
    """
    def __init__(self,*,context,start_document,identity_reader,code_checker,immutable_layout_reader,
                 metadata_reader,reservation_exchange,reservation_token,expected_broker_identity,clock,
                 launcher=None,fixture_key_reader=None):
        if type(context) is not ServiceContext or context.role!='px4' or type(start_document) is not RootPublicDocument or type(code_checker) is not CodeClosureChecker:
            raise SecurityError('Prepared P service inputs required')
        if start_document.path!=f'{APP}/public/{context.spec.session_uuid}-px4-start.json':raise SecurityError('Session start authority required')
        self.context,self.document,self.identity,self.code=context,start_document,identity_reader,code_checker
        self.layout,self.metadata=immutable_layout_reader,metadata_reader
        self.exchange,self._token,self.broker,self.clock=reservation_exchange,reservation_token,expected_broker_identity,clock
        self.launcher=launcher or DisabledOwnedLauncher();self.fixture_key=fixture_key_reader
        self.state='NEW';self.handle=self.expected=None

    def start(self):
        spec=self.context.spec
        try:
            if self.state!='NEW':raise SecurityError('Fresh P supervisor required')
            grant=self.document.read();now=finite(self.clock());spec.validate_time(now)
            fields={'version','approval_id','mode','session_uuid','epoch','spec_sha256','role_uid','identity','broker_identity','binary_path','key_sha256','key_epoch_timestamp','plan_sha256','issued_at','expires_at'}
            if set(grant)!=fields or type(grant['version']) is not int or grant['version']!=1:raise SecurityError('Start approval schema rejected')
            from .spec import uuid4 as validate_uuid
            validate_uuid(grant['approval_id'])
            if (grant['session_uuid'],grant['epoch'],grant['spec_sha256'],grant['role_uid'])!=(spec.session_uuid,spec.epoch,spec.digest,spec.px4_uid) or type(grant['role_uid']) is not int:
                raise SecurityError('Start scope rejected')
            issued,end=finite(grant['issued_at']),finite(grant['expires_at'])
            if not issued<=now<end or not 0<end-issued<=300 or issued<spec.issued_at or end>spec.expires_at:raise SecurityError('Start approval expired')
            mode=grant['mode']
            if mode not in ('fixture','live') or self.launcher.mode!=mode:raise SecurityError('Launcher provenance/mode rejected')
            if mode=='live' and os.geteuid()!=spec.px4_uid:raise SecurityError('Active P UID required')
            if mode=='live':
                from .runtime_contract import runtime_paths,validate_runtime_contract
                from .execution import Endpoint
                validate_runtime_contract(spec,Endpoint(runtime_paths(spec)['api'],spec.broker_uid,spec.agent_uid),self.metadata)
            identity=self.identity()
            if type(identity) is not RuntimeIdentity or identity.uid!=spec.px4_uid or identity.session_uuid!=spec.session_uuid or grant['identity']!=asdict(identity) or grant['broker_identity']!=asdict(self.broker):
                raise SecurityError('Start process identity rejected')
            layout=self.layout();self.code(identity,layout)
            binary=grant['binary_path']
            if binary!=APP+'/bin/px4' or not any(r['node'].path==binary and r['sha256']==spec.binary_sha256 for role in ('code','interpreter','library') for r in layout[role]):
                raise SecurityError('Approved secure PX4 image required')
            plan=startup_plan(spec,binary)
            if grant['plan_sha256']!=hashlib.sha256(canonical(plan)).hexdigest():raise SecurityError('Startup plan differs from approval')
            key_path=spec.runtime_dir+'/fs/mavlink/mavlink-signing-key.bin'
            chain=validate_private_chain(key_path,self.metadata,spec.px4_uid,leaf_kind='file',leaf_mode=0o600)
            if chain[-1].size!=40:raise SecurityError('Private P signing file invalid')
            if mode=='fixture':
                if self.fixture_key is None:raise SecurityError('Explicit nonoperational fixture required')
                raw=self.fixture_key()
            else:
                if self.fixture_key is not None:raise SecurityError('Fixture key reader forbidden in live mode')
                raw=checked_file(key_path,spec.px4_uid,mode=0o600,maximum=40)
            if type(raw) is not bytes or len(raw)!=40 or not any(raw[:32]) or hashlib.sha256(raw[:32]).hexdigest()!=grant['key_sha256']:
                raise SecurityError('Fresh P key fingerprint mismatch')
            epoch=integer(int.from_bytes(raw[32:],'little'),1,2**48-1)
            if epoch!=grant['key_epoch_timestamp'] or type(grant['key_epoch_timestamp']) is not int:raise SecurityError('P key epoch mismatch')
            for path in ancestors(spec.key_ledger_dir):
                node=self.metadata(path)
                if node.path!=path or node.uid not in (0,spec.px4_uid) or node.kind!='dir' or node.mode&0o022 or node.acl:raise SecurityError('Unsafe P ledger ancestor')
            validate_node(self.metadata(spec.key_ledger_dir),uid=spec.px4_uid,kind='dir',mode=0o700)
            try:self.metadata(spec.key_ledger_dir+'/'+grant['key_sha256'])
            except FileNotFoundError:pass
            else:raise SecurityError('P key marker already exists; never delete to retry')
            challenge=str(uuid4());peer,receipt=self.exchange(challenge)
            require_peer(peer,spec.broker_uid);value=unseal(self._token,receipt)
            expected={'version','mode','session_uuid','epoch','spec_sha256','key_sha256','identity','challenge','issued_at','ttl'}
            if set(value)!=expected or type(value['version']) is not int or value['version']!=1 or (value['mode'],value['session_uuid'],value['epoch'],value['spec_sha256'],value['key_sha256'],value['identity'],value['challenge'])!=(mode,spec.session_uuid,spec.epoch,spec.digest,grant['key_sha256'],asdict(self.broker),challenge):
                raise SecurityError('Original reservation receipt rejected')
            when,ttl=finite(value['issued_at']),finite(value['ttl']);now=finite(self.clock())
            if not 0<ttl<=1 or when<spec.issued_at or not when<=now<when+ttl or not issued<=now<end or self.identity()!=identity:
                raise SecurityError('Reservation/start permission expired')
            spec.validate_time(now)
            # Callback receives only the fixed reviewed plan and secure image.
            self._approved_plan=canonical(plan);self._approved_binary=binary;self._launch_end=end;self._receipt_end=when+ttl
            self._launch_consumed=False;self.state='AUTHORIZED'
            self.handle=self.launcher.launch(spec,binary,plan)
            self.expected=self.launcher.observe(self.handle)
            validate_observed_identity(spec,self.expected,self.expected)
            if self.expected.pid in (identity.pid,self.broker.pid):raise SecurityError('Child cannot be a supervisor/service')
            self.state='STARTED'
            return self.expected
        except Exception:
            if self.handle is not None and self.expected is not None:
                try:self.stop()
                except SecurityError:pass
            self.state='FAILED'
            raise SecurityError('P start blocked or failed; no fallback') from None

    def stop(self):
        if self.state=='STOPPED':return
        if self.handle is None:
            self.state='STOPPED';return
        observed=self.launcher.observe(self.handle)
        validate_observed_identity(self.context.spec,self.expected,observed)
        self.launcher.stop(self.handle)
        self.state='STOPPED'


class PosixOwnedLauncher:
    """Dormant actual launcher, reachable only from the validated P supervisor.

    No default selection/CLI activation. Every process is a registered direct
    child; signals are sent only after UID/path/birth/hash comparison. Methods
    are not invoked on the OS by this phase's tests or verification commands.
    """
    mode='live'
    def __init__(self,supervisor,metadata_reader):
        if type(supervisor) is not OwnedPX4Supervisor:raise SecurityError('Owned supervisor required')
        self.supervisor,self.metadata=supervisor,metadata_reader;self._children={}

    def launch(self,spec,binary,plan):
        import re,subprocess
        from pathlib import Path
        owner=self.supervisor
        if (owner.state!='AUTHORIZED' or owner.context.spec!=spec or os.geteuid()!=spec.px4_uid
                or owner._launch_consumed or owner._approved_binary!=binary or owner._approved_plan!=canonical(plan)
                or not finite(owner.clock())<min(owner._launch_end,owner._receipt_end)):raise SecurityError('Unconsumed P start approval required')
        # Existing PX4 calls system('/bin/sh '+rcS+' '+instance); avoid its
        # unquoted script-path issue without changing any of the 17 PX4 files.
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',spec.runtime_dir):raise SecurityError('Shell-safe private runtime path required')
        validate_node(self.metadata(spec.runtime_dir),uid=spec.px4_uid,kind='dir',mode=0o700)
        if binary!=APP+'/bin/px4' or hashlib.sha256(Path(binary).read_bytes()).hexdigest()!=spec.binary_sha256:
            raise SecurityError('Secure deployment image mismatch')
        owner._launch_consumed=True
        directory=os.open(spec.runtime_dir,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        log=None
        try:
            descriptor=os.open('rcS',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
            try:
                content=('#!/bin/sh\nset -eu\n'+'\n'.join(plan['commands'])+'\n').encode()
                view=memoryview(content)
                while view:
                    count=os.write(descriptor,view)
                    if count<=0:raise SecurityError('Startup script write failed')
                    view=view[count:]
                os.fsync(descriptor)
            finally:os.close(descriptor)
            descriptor=os.open('px4.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
            log=os.fdopen(descriptor,'wb',buffering=0);os.fsync(directory)
            environment={'PATH':'/usr/bin:/bin',**plan['environment']}
            spec.validate_time(owner.clock())
            if not finite(owner.clock())<min(owner._launch_end,owner._receipt_end):raise SecurityError('P start receipt expired before spawn')
            process=subprocess.Popen([binary,'-i',str(spec.instance_id),'-d','-s',spec.runtime_dir+'/rcS',
                '-w',spec.runtime_dir,spec.runtime_dir],cwd=spec.runtime_dir,env=environment,
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,shell=False)
            handle=object();self._children[handle]=dict(process=process,log=log,spec=spec,binary=binary,expected=None)
            log=None
            return handle
        except Exception:raise SecurityError('Owned launch failed; no retry or cleanup of ledger') from None
        finally:
            os.close(directory)
            if log is not None:log.close()

    def observe(self,handle):
        from .identity_checks import read_kernel_identity
        from .session import OwnedIdentity
        from pathlib import Path
        if handle not in self._children:raise SecurityError('Unknown process handle')
        item=self._children[handle];spec=item['spec'];process=item['process']
        if os.geteuid()!=spec.px4_uid:raise SecurityError('P role lost')
        snapshot=read_kernel_identity(process.pid)
        if snapshot['uid']!=spec.px4_uid or snapshot['ppid']!=os.getpid() or snapshot['path']!=item['binary']:
            raise SecurityError('Direct child identity lost')
        digest=hashlib.sha256(Path(item['binary']).read_bytes()).hexdigest()
        if digest!=spec.binary_sha256:raise SecurityError('Child image changed')
        identity=OwnedIdentity(process.pid,spec.px4_uid,snapshot['birth'],digest,spec.session_uuid)
        if item['expected'] is None:item['expected']=identity
        if identity!=item['expected']:raise SecurityError('Child PID reused')
        return identity

    def stop(self,handle):
        import subprocess
        if handle not in self._children:raise SecurityError('Unknown process handle')
        item=self._children[handle];process=item['process']
        if process.poll() is None:
            self.observe(handle)
            process.terminate()  # Only this registered, validated direct child.
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.observe(handle);process.kill();process.wait(timeout=1)
        item['log'].close();del self._children[handle]
