"""Fixed-path service preparation and bounded Broker event pump.

No live CLI, launchd registration, account operations, PX4 launcher or key reader.
Installed wrappers must bind trusted collectors/providers in immutable code.
"""
from dataclasses import dataclass
import os
import time
from .agent_activation import RootPublicDocument
from .access import validate_private_chain
from .bootstrap import BrokerBootstrap
from .platform_macos import checked_file
from .spec import SecureSessionSpec,SecurityError,strict_json,integer
from .execution import Deadline

APP='/Library/Application Support/AIDroneSecure'


@dataclass(frozen=True)
class ServiceContext:
    role:str
    spec:SecureSessionSpec
    manifest_path:str


def validate_service_argv(role,argv):
    if role not in ('broker','px4'):raise SecurityError('Fixed service role required')
    path=APP+'/private/'+role+'/manifest.json'
    if tuple(argv)!=('--manifest',path):raise SecurityError('Fixed manifest arguments required; no live/approve flags')
    return path


def prepare_role(role,argv,metadata):
    """Preparation is NOT approval. Role UID comes from root public config."""
    path=validate_service_argv(role,argv)
    matrix=RootPublicDocument(APP+'/public/roles.json',metadata).read()
    if set(matrix)!={'agent_uid','broker_uid','px4_uid'}:raise SecurityError('Root role matrix incomplete')
    for uid in matrix.values():integer(uid,1,2**31-1)
    if matrix['agent_uid']!=501 or 501 in (matrix['broker_uid'],matrix['px4_uid']) or len(set(matrix.values()))!=3:raise SecurityError('Independent roles required')
    uid=matrix[role+'_uid']
    if os.geteuid()!=uid:raise SecurityError('Service UID is not active')
    validate_private_chain(path,metadata,uid,leaf_kind='file',leaf_mode=0o600)
    raw=checked_file(path,uid,mode=0o600,maximum=16384)
    spec=SecureSessionSpec.from_dict(strict_json(raw,limit=16384))
    if matrix!={name:getattr(spec,name) for name in ('agent_uid','broker_uid','px4_uid')}:
        raise SecurityError('Private manifest/root UID matrix mismatch')
    return ServiceContext(role,spec,path)


def run_broker(context,bootstrap,readiness,*,monotonic=time.monotonic,maximum=128,lifetime=60):
    """Bounded pump, readiness callback polls only the already owned listener.

    Trial coordinator supplies an absolute external lifetime <=300 seconds;
    production supervision/lifetimes must be explicitly reviewed, not auto-retry.
    No actual socket/backend is selected by this function.
    """
    if type(context) is not ServiceContext or context.role!='broker' or type(bootstrap) is not BrokerBootstrap or context.spec!=bootstrap.provider.spec:
        raise SecurityError('Prepared broker context required')
    maximum=integer(maximum,1,128);lifetime=integer(lifetime,1,300);processed=0
    try:
        bootstrap.start()
        remaining=bootstrap.provider.approval_expires_at-bootstrap.provider.clock()
        end=monotonic()+min(lifetime,remaining)
        while processed<maximum:
            remaining=end-monotonic()
            if remaining<=0 or bootstrap.provider.clock()>=bootstrap.provider.approval_expires_at:raise SecurityError('Service lifetime expired')
            timeout=min(0.1,remaining)
            if readiness(bootstrap.listener,timeout):
                if end-monotonic()<=0:raise SecurityError('Service lifetime expired')
                bootstrap.poll_once();processed+=1
            if end-monotonic()<=0:raise SecurityError('Service lifetime expired')
        return dict(status='PASS',processed=processed,flight_authorized=False)
    except Exception:raise SecurityError('Bounded service pump stopped') from None
    finally:bootstrap.stop()
