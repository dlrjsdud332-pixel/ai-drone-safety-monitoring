"""Bounded key-free observation state; missing HOME/GPS is never invented."""
from .spec import SecurityError, finite, integer

FIELDS={'heartbeat','battery','home','position'}


class TelemetryState:
    def __init__(self, session_uuid, epoch, clock):
        self.clock=clock; self.reset(session_uuid,epoch)

    def reset(self, session_uuid, epoch):
        self.session_uuid=session_uuid; self.epoch=epoch
        self.sequence=0; self._received=None; self._data={}

    def ingest(self, envelope):
        if set(envelope) != {'session_uuid','epoch','sequence','issued_at','ttl','data'}:
            raise SecurityError('Invalid observation schema')
        if envelope['session_uuid'] != self.session_uuid or envelope['epoch'] != self.epoch:
            self._received=None; self._data={}
            raise SecurityError('Observation belongs to another session; explicit rebind required')
        now=finite(self.clock()); issued=finite(envelope['issued_at']); ttl=finite(envelope['ttl'])
        seq=integer(envelope['sequence'],1,2**53-1)
        if seq <= self.sequence or not 0 < ttl <= 5 or not issued <= now < issued+ttl:
            raise SecurityError('Stale/replayed observation')
        data=envelope['data']
        if not isinstance(data,dict) or set(data)-FIELDS:
            raise SecurityError('Unexpected telemetry field')
        if 'heartbeat' in data and type(data['heartbeat']) is not bool:
            raise SecurityError('Invalid heartbeat')
        for name in ('home','position'):
            value=data.get(name)
            if value is not None:
                if not isinstance(value,dict) or set(value)!={'lat','lon','alt_msl'}:
                    raise SecurityError('Invalid position')
                if not -90 <= finite(value['lat']) <= 90 or not -180 <= finite(value['lon']) <= 180:
                    raise SecurityError('Invalid coordinate')
                finite(value['alt_msl'])
        if data.get('battery') is not None and not 0 <= finite(data['battery']) <= 100:
            raise SecurityError('Invalid battery')
        import copy
        self._data=copy.deepcopy(data); self.sequence=seq; self._received=now; self._expires=issued+ttl

    def snapshot(self):
        import copy
        now=finite(self.clock())
        fresh=(self._received is not None and self._received <= now < self._expires)
        data=self._data if fresh else {}
        return dict(session_uuid=self.session_uuid,epoch=self.epoch,sequence=self.sequence,
            telemetry_source='PX4',telemetry_transport='BROKER',monitor_only=True,
            connected=bool(data.get('heartbeat',False)),fresh=fresh,
            home=copy.deepcopy(data.get('home')),position=copy.deepcopy(data.get('position')),
            battery=data.get('battery'),flight_ready=False,flight_authorized=False)
