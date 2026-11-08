"""Pure evidence evaluators. Saved data never grants runtime authority."""
from .spec import SecurityError


def validate_socket_evidence(spec, identity, records):
    expected={f'127.0.0.1:{l.receiver_port}' for l in spec.links}
    if (len(records)!=2 or any(r.get('pid')!=identity.pid or r.get('P')!='UDP' or r.get('t')!='IPv4' for r in records)
            or {r.get('n') for r in records}!=expected):
        raise SecurityError('Exact two owned loopback UDP sockets required')
    return dict(status='PASS',scope='SUPPLIED_EVIDENCE_ONLY_NOT_LIVE_AUTHORITY')


def evaluate_module_state(spec, links):
    if len(links)!=2:raise SecurityError('Two complete module snapshots required')
    seen=set()
    for row in links:
        if set(row)!={'receiver_port','client_port','mavlink_version','ftp','forwarding','signing_initialized','strict','receive_enabled'}:
            raise SecurityError('Incomplete module snapshot')
        if (type(row['mavlink_version']) is not int or row['mavlink_version']!=2
                or row['ftp'] is not False or row['forwarding'] is not False
                or row['signing_initialized'] is not True or row['strict'] is not True or row['receive_enabled'] is not True):
            raise SecurityError('Strict authenticated module policy not confirmed')
        pair=(row['receiver_port'],row['client_port'])
        if pair in seen:raise SecurityError('Duplicate module snapshot')
        seen.add(pair)
    if seen!={(l.receiver_port,l.client_port) for l in spec.links}:raise SecurityError('Module endpoint mismatch')
    return dict(status='PASS',scope='SUPPLIED_EVIDENCE_ONLY_NOT_LIVE_AUTHORITY')
