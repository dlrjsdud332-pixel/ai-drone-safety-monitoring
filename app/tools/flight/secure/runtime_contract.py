"""Pinned per-session runtime contract. Metadata only; no directory creation."""
from .access import validate_node, ACE
from .spec import SecurityError, private_path

CODE_ROOT='/Library/Application Support/AIDroneSecure'
DATA_ROOT='/private/var/lib/aidrone/runtime'


def runtime_paths(spec):
    root=DATA_ROOT+'/'+spec.session_uuid
    return {'root':root,'px4':root+'/p','broker':root+'/b','api':root+'/b/api.sock'}


def validate_runtime_contract(spec,endpoint,reader):
    paths=runtime_paths(spec)
    if spec.runtime_dir!=paths['px4'] or endpoint.path!=paths['api']:
        raise SecurityError('Pinned session runtime required')
    from pathlib import PurePosixPath
    for role,uid in (('px4',spec.px4_uid),('broker',spec.broker_uid)):
        target=paths[role];private_path(target)
        for path in reversed(PurePosixPath(target).parents):
            if str(path)=='/':continue
            node=reader(str(path))
            if node.path!=str(path):raise SecurityError('Runtime metadata path mismatch')
            validate_node(node,uid=0,kind='dir',mode=0o755)
        node=reader(target)
        if node.path!=target:raise SecurityError('Runtime role path mismatch')
        validate_node(node,uid=uid,kind='dir',mode=0o700,
                      acl=(ACE(spec.agent_uid,frozenset({'search'})),) if role=='broker' else ())
    return dict(status='PASS',scope='METADATA_CONTRACT_ONLY',runtime='UNVERIFIED',paths=paths)
