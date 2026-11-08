"""Trusted service lifecycle over injected handles only; never launches PX4.

No service registration, Popen, signals, filesystem cleanup or runtime CLI.
"""
from dataclasses import asdict
from .activation import TrustedActivationProvider
from .execution import BoundDatagrams,Listener
from .ipc_transport import IPCServerTransport
from .broker import OfflineBroker
from .session import validate_observed_identity
from .spec import SecurityError


class GatedFixtureBackend:
    """Only fixture grants may reach this injected memory backend."""
    def __init__(self,provider,backend):self.provider,self.backend=provider,backend
    def _create(self,operation,method):
        try:
            permit=self.provider.authorize(operation)
            self.provider.consume_socket_permit(permit,operation,mode='fixture')
            return getattr(self.backend,method)()
        except Exception:
            self.provider.close();raise SecurityError('Fixture allocation blocked') from None
    def stream(self):return self._create('connect_ipc','stream')
    def listener(self):return self._create('listen_ipc','listener')
    def datagram(self):return self._create('bind_loopback_udp','datagram')


class BrokerBootstrap:
    """One session, no automatic restart/fallback, no probe at service start.

    Fixed probe adapter is constructed separately by the authorized nonflight
    trial coordinator. OfflineBroker remains observe/status/probe_plan only.
    """
    def __init__(self,provider,backend,api_token,ownership_check):
        if type(provider) is not TrustedActivationProvider or provider.role!='broker':raise SecurityError('Broker provider required')
        import hashlib,hmac
        if (type(api_token) is not bytes or not any(api_token) or len(api_token)<32 or hmac.compare_digest(api_token,provider.trust._secret)
                or hmac.compare_digest(api_token,provider._verifier._token) or hashlib.sha256(api_token).digest()==provider._fingerprint):
            provider.close();raise SecurityError('Independent API capability required')
        self.provider,self.backend,self._token,self._ownership=provider,backend,api_token,ownership_check
        self.listener=self.datagrams=None;self.state='NEW'

    def start(self):
        try:
            if self.state!='NEW':raise SecurityError('Fresh bootstrap required')
            # Complete preflight and ledger agreement before ANY creation.
            permit=self.provider.authorize('listen_ipc')
            self.provider.check_permit(permit,'listen_ipc',mode=permit.mode)
            spec=self.provider.spec
            broker=OfflineBroker(spec,self._token,self.provider.clock)
            server=IPCServerTransport(broker,session_claims=self.provider._lease)
            self.listener=Listener(self.provider.endpoint,self.provider._metadata_reader,server,self.backend)
            self.datagrams=BoundDatagrams(spec,self._ownership,self.backend)
            self.listener.open();channels=self.datagrams.open()
            self.state='READY'
            return dict(status='PASS',scope='STARTED_INJECTED_SERVICE',channels=len(channels),flight_authorized=False)
        except Exception:
            self.stop();self.state='FAILED'
            raise SecurityError('Bootstrap failed closed') from None

    def poll_once(self):
        try:
            if self.state!='READY':raise SecurityError('Service not ready')
            # Approval/current state revalidation also applies after start.
            self.provider.authorize('listen_ipc')
            self.listener.poll_once()
        except Exception:
            self.stop();self.state='FAILED'
            raise SecurityError('Service stopped after validation/transport failure') from None

    def stop(self):
        self.provider.close()
        for handle in (self.listener,self.datagrams):
            if handle is not None:
                try:handle.close()
                except Exception:pass
        if self.listener is not None and self.listener.server is not None:
            self.listener.server.invalidate()
        if self.state!='FAILED':self.state='STOPPED'


def owned_cleanup_plan(spec,expected,observed,paths):
    """Reviewable plan only, never kill/unlink. Never includes key/ledger."""
    validate_observed_identity(spec,expected,observed)
    allowed={spec.runtime_dir+'/p.sock',spec.runtime_dir+'/p.lock'}
    if not isinstance(paths,tuple) or not set(paths)<=allowed:raise SecurityError('Cleanup outside owned runtime rejected')
    return dict(mode='PLAN_ONLY_EXPLICIT_APPROVAL_REQUIRED',pid=expected.pid,birth=expected.birth,
                paths=list(paths),key_or_ledger_cleanup=False,executed=False)


def owned_service_cleanup_plan(provider,expected,observed,recorded_node,current_node):
    from dataclasses import replace
    from .access import ACE,validate_node
    from .identity_checks import RuntimeIdentity
    if (type(expected) is not RuntimeIdentity or expected!=observed or expected.uid!=provider.spec.broker_uid
            or expected.session_uuid!=provider.spec.session_uuid or expected.pid<=0 or not expected.birth):
        raise SecurityError('Owned service identity required')
    if recorded_node.path!=provider.endpoint.path or replace(recorded_node,captured_at=None)!=replace(current_node,captured_at=None):
        raise SecurityError('Replaced endpoint cannot be cleaned')
    validate_node(current_node,uid=provider.spec.broker_uid,kind='socket',mode=0o600,
                  acl=(ACE(provider.spec.agent_uid,frozenset({'read','write'})),))
    return dict(mode='PLAN_ONLY_EXPLICIT_APPROVAL_REQUIRED',pid=expected.pid,birth=expected.birth,
                paths=[current_node.path],key_or_ledger_cleanup=False,executed=False)
