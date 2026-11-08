"""Memory broker fixture using the unchanged UDP simulator JSON field names."""
import time
from uuid import uuid4
from ..flight.secure.spec import SecureSessionSpec,LinkSpec,SecurityError
from ..flight.secure.broker import OfflineBroker
from ..flight.secure.client import ObservationClient
from ..flight.secure.platform_macos import PeerCredentials


class DemoSource:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.count=0;self._bind()
    def _bind(self):
        now=self.clock();self.bound=now;self.sequence=0
        self.spec=SecureSessionSpec(str(uuid4()),str(uuid4()),220,221,1,246,191,501,602,601,now,now+300,'/private/var/lib/aidrone/demo/p','/private/var/lib/aidrone/demo-ledger','a'*64,'b'*64,(LinkSpec(41001,41002,31),LinkSpec(41003,41004,32)))
        token=b'D'*32  # Public non-operational API fixture; no MAVLink key.
        self.broker=OfflineBroker(self.spec,token,self.clock)
        self.client=ObservationClient(self.spec,token,lambda raw:(PeerCredentials(602,602),self.broker.dispatch(PeerCredentials(501,20),raw)),self.clock)
    def poll(self):
        if self.clock()-self.bound>=100:self._bind()  # Explicit demo-only session rotation, below request budget.
        self.sequence+=1;self.count+=1
        packet=dict(lat=37.5665,lon=126.9780,altitude_m=30.,battery=85.,flight_mode='DEMO HOLD',mission='DEMO OBSERVE',mission_status='FIXTURE',mission_progress=None)
        point=dict(lat=packet['lat'],lon=packet['lon'],alt_msl=packet['altitude_m'])
        self.broker.telemetry.ingest(dict(session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,sequence=self.sequence,issued_at=self.clock(),ttl=2.,data=dict(heartbeat=True,position=point,battery=packet['battery'])))
        return packet


def require_offline(mode):
    if mode!='OFFLINE_DEMO':raise SecurityError('SECURE_LIVE BLOCKED: production activation/source/deployment not verified')
