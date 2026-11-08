"""Key-free UI observation projection. No transport or flight API."""
import copy
import time
from ..flight.secure.spec import SecurityError,finite,uuid4


class ObservationModel:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.session=None;self.data=None;self.received=None;self.last_good=None
        self.error=None;self.gate='UNVERIFIED';self._sequence=-1;self.details=None;self.broker_expires=None
    def clear(self,error=None):
        self.data=None;self.received=None;self.details=None;self.broker_expires=None;self.error=error
        self.gate='BLOCKED' if error else 'UNVERIFIED'
    def update(self,client):
        try:
            session=(client.spec.session_uuid,client.spec.epoch)
            if session!=self.session:
                self.clear();self.last_good=None;self._sequence=-1;self.session=session
            data=client.observe()  # ObservationClient performs HMAC/peer/schema checks.
            if (data['session_uuid'],data['epoch'])!=session or data['sequence']<=self._sequence:raise SecurityError('Observation replay')
            self._sequence=data['sequence']
            if not data['connected'] or not data['fresh']:
                from ..flight.secure.telemetry_feed import TelemetryClient
                from ..flight.secure.telemetry_exporter import NativeTelemetryClient
                failure=(client.details or {}).get('error') if type(client) in (TelemetryClient,NativeTelemetryClient) else None
                self.clear(failure)
                if failure is None:self.broker_expires=finite(self.clock())+.5
                return
            from ..flight.secure.telemetry_feed import TelemetryClient
            from ..flight.secure.telemetry_exporter import NativeTelemetryClient
            self.details=copy.deepcopy(client.details) if type(client) in (TelemetryClient,NativeTelemetryClient) else None
            self.data=copy.deepcopy(data);self.received=finite(self.clock());self.last_good=self.received
            self.broker_expires=self.received+(.5 if self.details is not None else 2)
            self.error=None;self.gate='UNVERIFIED'  # Demo transport never grants a live PASS.
        except Exception:self.clear('AUTH_OR_BROKER_FAILURE')
    def snapshot(self):
        now=finite(self.clock());fresh=self.received is not None and 0<=now-self.received<2
        if self.details is not None:fresh=fresh and now<self.details['valid_until']
        data=self.data if fresh and self.error is None else None
        channel=self.broker_expires is not None and now<self.broker_expires and self.error is None
        return dict(channel_authenticated=channel,data=copy.deepcopy(data),telemetry=copy.deepcopy(self.details) if data else None,fresh=bool(data),gate=self.gate if fresh or self.error else 'UNVERIFIED',error=self.error,session=self.session,last_good=self.last_good,last_good_age=None if self.last_good is None else max(0,now-self.last_good),age=None if self.received is None else max(0,now-self.received),authenticated_fixture=bool(channel),broker='MEMORY_FIXTURE' if channel else 'UNKNOWN',real_drone_connected=False)
