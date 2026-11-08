"""Observation client with an injected authenticated transport, no key/file/socket API."""
from threading import RLock
from uuid import uuid4
from .policy import seal, unseal, OPERATIONS
from .spec import canonical as canonical_data
from .platform_macos import require_peer
from .spec import SecurityError, finite, integer


class ObservationClient:
    def __init__(self,spec,api_token,exchange,clock):
        self.spec=spec; self._token=api_token; self._exchange=exchange; self.clock=clock
        self._sequence=0; self._lock=RLock()

    def request(self, operation):
        with self._lock:
            if not isinstance(operation,str) or operation not in OPERATIONS:raise SecurityError('Observation operation required')
            self.spec.validate_time(self.clock()); self._sequence+=1
            request_id=str(uuid4())
            body=dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,
                sequence=self._sequence,request_id=request_id,issued_at=self.clock(),ttl=2.0,operation=operation)
            # Trusted adapter must return the OS credential, never a body UID.
            peer, response=self._exchange(seal(self._token,body))
            require_peer(peer,self.spec.broker_uid)
            result=unseal(self._token,response)
            if set(result)!={'version','session_uuid','epoch','sequence','request_id','issued_at','ttl','operation','data'}:
                raise SecurityError('Invalid response schema')
            for field in ('version','session_uuid','epoch','sequence','request_id','operation'):
                if type(result[field]) is not type(body[field]) or result[field]!=body[field]:
                    raise SecurityError('Response correlation failed')
            issued=finite(result['issued_at']); ttl=finite(result['ttl']); now=finite(self.clock())
            if not 0 < ttl <= 5 or not issued <= now < issued+ttl:
                raise SecurityError('Response expired')
            self.spec.validate_time(now)
            data=result['data']
            self._validate_data(operation,data)
            return data

    def _validate_data(self, operation, data):
        if not isinstance(data,dict):raise SecurityError('Typed response data required')
        if operation=='status':
            expected=dict(mode='OFFLINE_ONLY',packets_sent=0,px4_commands_sent=0,runtime_authorized=False,capability='monitor_only')
            if canonical_data(data)!=canonical_data(expected):raise SecurityError('Invalid status result')
        elif operation=='probe_plan':
            if set(data)!={'mode','operations','packets_sent'} or data['mode']!='PLAN_ONLY' or type(data['packets_sent']) is not int or data['packets_sent']!=0:
                raise SecurityError('Invalid plan result')
            if data['operations']!=['signed_before','unsigned','wrong_key','replay_same_link','replay_other_link','signed_after']:
                raise SecurityError('Invalid probe plan')
        else:
            expected={'session_uuid','epoch','sequence','telemetry_source','telemetry_transport','monitor_only','connected','fresh','home','position','battery','flight_ready','flight_authorized'}
            if set(data)!=expected or data['session_uuid']!=self.spec.session_uuid or data['epoch']!=self.spec.epoch:
                raise SecurityError('Invalid observation result')
            if data['telemetry_source']!='PX4' or data['telemetry_transport']!='BROKER' or data['monitor_only'] is not True or data['flight_ready'] is not False or data['flight_authorized'] is not False:
                raise SecurityError('Observation capability cannot grant flight permission')
            integer(data['sequence'],0,2**53-1)
            if type(data['connected']) is not bool or type(data['fresh']) is not bool or (data['connected'] and not data['fresh']):
                raise SecurityError('Invalid observation freshness')
            if not data['fresh'] and any(data[name] is not None for name in ('home','position','battery')):
                raise SecurityError('Expired observation retained live fields')
            if data['battery'] is not None and not 0 <= finite(data['battery']) <= 100:
                raise SecurityError('Invalid observation battery')
            for field in ('home','position'):
                item=data[field]
                if item is not None:
                    if not isinstance(item,dict) or set(item)!={'lat','lon','alt_msl'} or not -90 <= finite(item['lat']) <= 90 or not -180 <= finite(item['lon']) <= 180:
                        raise SecurityError('Invalid observation coordinate')
                    finite(item['alt_msl'])

    def observe(self):return self.request('observe')
    def status(self):return self.request('status')
    def probe_plan(self):return self.request('probe_plan')
