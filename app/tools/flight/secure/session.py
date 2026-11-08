"""Offline launch planning. No Popen, socket, signals or service APIs."""
from dataclasses import dataclass
import shlex
from .spec import SecurityError, private_path


@dataclass(frozen=True)
class OwnedIdentity:
    pid: int
    uid: int
    birth: str
    binary_sha256: str
    session_uuid: str


def validate_observed_identity(spec, expected, observed):
    if (not isinstance(expected,OwnedIdentity) or expected != observed or expected.pid <= 0
            or expected.uid != spec.px4_uid or expected.session_uuid != spec.session_uuid
            or expected.binary_sha256 != spec.binary_sha256):
        raise SecurityError('Owned identity lost; no commands or signals are permitted')


def startup_plan(spec, binary_path):
    private_path(binary_path)
    if not binary_path.endswith('/px4'):
        raise SecurityError('Explicit secure PX4 binary required')
    build=binary_path.rsplit('/bin/px4',1)
    if len(build)!=2 or build[1]:raise SecurityError('Expected explicit bin/px4 layout')
    bin_dir=build[0]+'/bin'
    client=lambda name: shlex.quote(bin_dir+'/px4-'+name)+' --instance '+str(spec.instance_id)
    commands=[client('param')+' set MAV_SYS_ID '+str(spec.vehicle_system),
              client('param')+' set MAV_PROTO_VER 2',client('dataman')+' start -r']
    commands.extend(client('mavlink')+f' start -n lo -u {l.receiver_port} -o {l.client_port} -t 127.0.0.1 -m onboard -r 10000' for l in spec.links)
    commands.append(client('mavlink')+' status')
    return dict(mode='PLAN_ONLY_R3_APPROVAL_REQUIRED',session_uuid=spec.session_uuid,
        commands=commands,environment=dict(PX4_SECURE_SESSION_UUID=spec.session_uuid,PX4_SECURE_EPOCH=spec.epoch,PX4_SECURE_RUNTIME=spec.runtime_dir,PX4_SECURE_KEY_LEDGER=spec.key_ledger_dir,
            PX4_SECURE_CONTROLLER_SYSTEM=str(spec.controller_system),PX4_SECURE_CONTROLLER_COMPONENT=str(spec.controller_component)),
        executed=False,packets_sent=0,flight_commands_sent=0)
