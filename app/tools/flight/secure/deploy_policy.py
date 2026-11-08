"""Declarative deployment and launchd static checks. Never install/chown/chmod."""
from pathlib import PurePosixPath
import plistlib
from .access import validate_node
from .spec import SecurityError, private_path


PRIVATE_MODES={'key':('file',0o600),'manifest':('file',0o600),
               'ledger':('dir',0o700),'px4_ipc':('socket',0o600)}


def validate_layout(spec, entries):
    """All code/ancestors/interpreter/libraries must be trusted root-owned.

    Entries are authoritative collected metadata plus independently computed
    hashes, not boolean assertions. Completeness of loaded dylibs remains live.
    """
    required={'code','interpreter','library','key','manifest','ledger','px4_ipc'}
    if not isinstance(entries,dict) or set(entries)!=required:raise SecurityError('Complete deployment evidence required')
    paths=[]
    for role,records in entries.items():
        if not isinstance(records,tuple) or not records:raise SecurityError('Deployment evidence missing')
        for record in records:
            if set(record)!={'node','ancestors','sha256','expected_sha256'}:raise SecurityError('Invalid deployment evidence')
            node=record['node']; paths.append(node.path)
            private_path(node.path)
            # The Desktop, /tmp, user site-packages and Conda paths are never
            # accepted as service installation roots even if mode looks safe.
            system_library=role=='library' and node.path.startswith(('/usr/lib/','/System/Library/'))
            from .runtime_contract import runtime_paths
            runtime=runtime_paths(spec)['px4']
            runtime_private=(spec.runtime_dir==runtime and ((role=='key' and node.path==runtime+'/fs/mavlink/mavlink-signing-key.bin') or (role=='px4_ipc' and node.path==runtime+'/p.sock')))
            if not system_library and not runtime_private and not node.path.startswith('/Library/Application Support/AIDroneSecure/'):
                raise SecurityError('Trusted installation root required')
            chain=tuple(str(p) for p in reversed(PurePosixPath(node.path).parents) if str(p)!='/')
            ancestors=record['ancestors']
            if tuple(n.path for n in ancestors)!=chain:raise SecurityError('Complete ancestor evidence required')
            owner=0
            if role in PRIVATE_MODES:
                if runtime_private:
                    owner=spec.px4_uid
                elif node.path.startswith('/Library/Application Support/AIDroneSecure/private/broker/'):
                    owner=spec.broker_uid
                elif node.path.startswith('/Library/Application Support/AIDroneSecure/private/px4/'):
                    owner=spec.px4_uid
                else:raise SecurityError('Explicit private role directory required')
                if role=='px4_ipc' and owner!=spec.px4_uid:raise SecurityError('PX4 IPC role mismatch')
                if spec.runtime_dir==runtime and owner==spec.px4_uid and role in ('key','px4_ipc') and not runtime_private:
                    raise SecurityError('Pinned PX4 runtime resource required')
            for parent in ancestors:
                if (parent.uid not in (0,owner) or parent.kind!='dir' or parent.mode&0o022 or parent.acl):
                    raise SecurityError('Unsafe deployment ancestor')
                validate_node(parent,uid=parent.uid,kind='dir',mode=parent.mode)
            if role in PRIVATE_MODES:
                validate_node(ancestors[-1],uid=owner,kind='dir',mode=0o700)
                kind,mode=PRIVATE_MODES[role]
                validate_node(node,uid=owner,kind=kind,mode=mode)
            else:
                mode=0o555 if role in ('code','interpreter') else 0o444
                if system_library:
                    if node.mode not in (0o444,0o555,0o644,0o755):raise SecurityError('System library mode rejected')
                    mode=node.mode
                validate_node(node,uid=0,kind='file',mode=mode)
                expected=record['expected_sha256']; observed=record['sha256']
                if (type(expected) is not str or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected) or observed!=expected):
                    raise SecurityError('Immutable artifact hash mismatch')
    for role in ('key','manifest','ledger'):
        if {record['node'].uid for record in entries[role]}!={spec.broker_uid,spec.px4_uid}:
            raise SecurityError('Both private role copies required')
    if len(paths)!=len(set(paths)):raise SecurityError('Deployment path role collision')
    return dict(status='PASS',scope='COLLECTED_LAYOUT_ONLY',installed=False,runtime='UNVERIFIED')


def validate_launchd(raw, *, role, user, group, executable, manifest):
    """No shell, runtime secret/env injection, root role or auto-restart."""
    value=plistlib.loads(raw)
    if role not in ('broker','px4') or user not in ('_aidrone_broker','_aidrone_px4') or user!='_aidrone_'+role or group!=user:
        raise SecurityError('Service role rejected')
    for path in (executable,manifest):
        private_path(path)
        if not path.startswith('/Library/Application Support/AIDroneSecure/'):
            raise SecurityError('Trusted launch path required')
    expected=dict(Label='com.aidrone.secure-'+role,UserName=user,GroupName=group,
                  ProgramArguments=[executable,'--manifest',manifest],RunAtLoad=False,KeepAlive=False,
                  ProcessType='Background',WorkingDirectory='/Library/Application Support/AIDroneSecure',
                  EnvironmentVariables={'PATH':'/usr/bin:/bin','PYTHONNOUSERSITE':'1','PYTHONPATH':'','DYLD_LIBRARY_PATH':''},
                  Umask=0o077,ExitTimeOut=3)
    if type(value) is not dict or value!=expected:raise SecurityError('Launchd static policy rejected')
    # Exact typed equality: bool must not stand in for int or vice versa.
    if type(value['RunAtLoad']) is not bool or type(value['KeepAlive']) is not bool or type(value['Umask']) is not int or type(value['ExitTimeOut']) is not int:
        raise SecurityError('Launchd field type rejected')
    return dict(status='PASS',scope='STATIC_TEMPLATE_ONLY',installed=False)
