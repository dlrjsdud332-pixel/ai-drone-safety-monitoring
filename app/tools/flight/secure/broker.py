"""In-memory broker dispatcher. There is deliberately no socket/send API."""
from .policy import RequestPolicy
from .telemetry import TelemetryState


class OfflineBroker:
    def __init__(self, spec, api_token, clock):
        self.spec=spec; self.clock=clock
        self.policy=RequestPolicy(spec,api_token,clock)
        self.telemetry=TelemetryState(spec.session_uuid,spec.epoch,clock)

    def dispatch(self, peer, request):
        value=self.policy.accept(peer,request)
        if value['operation']=='observe':
            data=self.telemetry.snapshot()
        elif value['operation']=='status':
            data=dict(mode='OFFLINE_ONLY',packets_sent=0,px4_commands_sent=0,
                runtime_authorized=False,capability='monitor_only')
        else:
            data=dict(mode='PLAN_ONLY',operations=['signed_before','unsigned','wrong_key','replay_same_link','replay_other_link','signed_after'],packets_sent=0)
        return self.policy.respond(dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,
            sequence=value['sequence'],request_id=value['request_id'],issued_at=self.clock(),ttl=2.0,
            operation=value['operation'],data=data))
