"""Receive-only decoded P telemetry over authenticated supplied channels.

No MAVLink key/parser, socket, transmit method or default live source. Immutable
P-side code must bind actual current telemetry and kernel peer identity.
"""
import copy
from .spec import SecurityError,finite,integer,uuid4
from .policy import seal,unseal
from .platform_macos import require_peer
from .session import validate_observed_identity
from .broker import OfflineBroker
from .client import ObservationClient
from ...gazebo.mavlink_observer import MavlinkObserver

TYPES={'HEARTBEAT','GPS_RAW_INT','GLOBAL_POSITION_INT','SYS_STATUS','BATTERY_STATUS','MISSION_CURRENT'}
FIELDS={'HEARTBEAT':{'autopilot','type','base_mode','custom_mode'},'GPS_RAW_INT':{'fix_type','satellites_visible','lat','lon','alt'},'GLOBAL_POSITION_INT':{'lat','lon','alt','relative_alt','hdg','time_boot_ms'},'SYS_STATUS':{'battery_remaining'},'BATTERY_STATUS':{'battery_remaining'},'MISSION_CURRENT':{'seq','total','mission_state'}}
MODE={1:'MANUAL',2:'ALTCTL',3:'POSCTL',5:'ACRO',6:'OFFBOARD',7:'STABILIZED',8:'RATTITUDE'}
AUTO={1:'READY',2:'TAKEOFF',3:'LOITER',4:'MISSION',5:'RTL',6:'LAND',8:'FOLLOW_TARGET',9:'PRECLAND'}

class Message:
    def __init__(self,row):self.row=row;self.__dict__.update(row['fields'])
    def get_type(self):return self.row['type']
    def get_srcSystem(self):return self.row['system']
    def get_srcComponent(self):return self.row['component']
    def to_dict(self):return copy.deepcopy(self.row['fields'])


class TelemetryReceiver:
    """Broker side: P peer/HMAC/session/sequence plus per-message expiry."""
    def __init__(self,spec,token,identity,clock):
        if type(token) is not bytes or len(token)!=32 or not any(token):raise SecurityError('Independent telemetry capability required')
        self.spec,self.token,self.identity,self.clock=spec,token,identity,clock
        self.observer=MavlinkObserver(spec.vehicle_system,spec.vehicle_component)
        from ...gazebo.px4_position import Px4Position
        self.position=Px4Position();self.sequence=0;self.closed=False;self.deadlines={};self.mission=None
    def close(self):
        self.closed=True;self.observer.reset();self.position.latest.clear();self.position.times.clear();self.deadlines.clear();self.mission=None
    def reset(self,session_uuid,epoch):
        # Transport shutdown is terminal; reset must never revive a session.
        self.close()
    def accept(self,peer,raw,nonce):
        try:
            if self.closed:raise SecurityError('Telemetry requires explicit fresh-session rebind')
            require_peer(peer,self.spec.px4_uid);uuid4(nonce);v=unseal(self.token,raw);now=finite(self.clock());self.spec.validate_time(now)
            if set(v)!={'version','session_uuid','epoch','challenge','sequence','issued_at','ttl','identity','messages'} or type(v['version']) is not int or v['version']!=1:raise SecurityError('Telemetry schema rejected')
            if (v['session_uuid'],v['epoch'],v['challenge'])!=(self.spec.session_uuid,self.spec.epoch,nonce):raise SecurityError('Telemetry correlation rejected')
            from .session import OwnedIdentity
            validate_observed_identity(self.spec,self.identity,OwnedIdentity(**v['identity']))
            if self.sequence and now>=self.deadlines.get('HEARTBEAT',-1):raise SecurityError('Lost heartbeat requires fresh session')
            seq=integer(v['sequence'],1,2**53-1);issued=finite(v['issued_at']);ttl=finite(v['ttl'])
            if seq!=self.sequence+1 or not 0<ttl<=1 or not self.spec.issued_at<=issued<=now<issued+ttl:raise SecurityError('Telemetry replay/stale')
            rows=v['messages']
            if type(rows) is not list or not 1<=len(rows)<=6 or len({r['type'] for r in rows})!=len(rows):raise SecurityError('Bounded unique message batch required')
            for r in rows:
                if set(r)!={'type','system','component','captured_at','fields'} or r['type'] not in TYPES or type(r['fields']) is not dict:raise SecurityError('Read-only telemetry types required')
                if type(r['system']) is not int or type(r['component']) is not int or (r['system'],r['component'])!=(self.spec.vehicle_system,self.spec.vehicle_component):raise SecurityError('Vehicle mismatch')
                stamp=finite(r['captured_at'])
                if not issued-1<=stamp<=now or now-stamp>=1:raise SecurityError('Message capture stale')
                kind=r['type']
                if set(r['fields'])!=FIELDS[kind] or any(type(value) is not int for value in r['fields'].values()):raise SecurityError('Fixed decoded message fields required')
                msg=Message(r);mode=None
                if kind in ('GPS_RAW_INT','GLOBAL_POSITION_INT'):
                    integer(msg.lat,-900000000,900000000);integer(msg.lon,-1800000000,1800000000);integer(msg.alt,-2**31,2**31-1)
                if kind=='GLOBAL_POSITION_INT':
                    integer(msg.relative_alt,-2**31,2**31-1);integer(msg.hdg,0,65535);integer(msg.time_boot_ms,0,2**32-1)
                if kind=='GPS_RAW_INT':
                    integer(msg.fix_type,0,8);integer(msg.satellites_visible,0,255)
                if kind=='HEARTBEAT':
                    custom=integer(msg.custom_mode,0,2**32-1);main=(custom>>16)&255;sub=(custom>>24)&255
                    mode=AUTO.get(sub,'UNKNOWN') if main==4 else MODE.get(main,'UNKNOWN')
                if kind in ('SYS_STATUS','BATTERY_STATUS'):
                    integer(msg.battery_remaining,-1,100)
                    if msg.battery_remaining==-1:self.observer.latest.pop('battery',None)
                packet=self.observer.ingest(msg,stamp,mode=mode)
                if packet is None:raise SecurityError('PX4 heartbeat/source not established')
                self.position.ingest(packet,stamp);self.deadlines[kind]=min(stamp+2,issued+ttl)
                if kind=='MISSION_CURRENT':
                    total=integer(r['fields'].get('total',0),0,65535);index=integer(msg.seq,0,65535);state=integer(r['fields'].get('mission_state',0),0,6)
                    if total and index>=total:raise SecurityError('Mission index out of range')
                    self.mission=dict(sequence=index,total=total or None,state=state,progress=100 if total and state==5 else min(99,int(100*index/total)) if total and state in (2,3,4) else None,progress_basis='MISSION_ITEM_INDEX')
            self.sequence=seq
        except Exception:
            self.close();raise SecurityError('Telemetry authentication/source blocked') from None
    def snapshot(self):
        now=finite(self.clock());live=not self.closed and self.spec.issued_at<=now<self.spec.expires_at and now<self.deadlines.get('HEARTBEAT',-1)
        if not live:
            # A lost established heartbeat permanently closes this session.
            if self.sequence:self.close()
        fresh=lambda k: live and now<self.deadlines.get(k,-1)
        snap=self.observer.snapshot(now);pos=self.position.snapshot(now,live)
        gps=pos['gps_valid'] and fresh('GPS_RAW_INT') and fresh('GLOBAL_POSITION_INT')
        battery=self.observer.latest.get('battery') if (fresh('SYS_STATUS') or fresh('BATTERY_STATUS')) else None
        # MavlinkObserver exports battery under battery when independently fresh.
        return dict(session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,sequence=self.sequence,connected=live,fresh=live,position=dict(lat=pos['lat'],lon=pos['lon'],alt_msl=snap.get('global_alt_msl_m')) if gps else None,battery=battery,flight_mode=snap['flight_mode'] if live else None,relative_altitude_m=snap.get('relative_altitude_m') if fresh('GLOBAL_POSITION_INT') else None,mission=copy.deepcopy(self.mission) if fresh('MISSION_CURRENT') else None,error='SOURCE_BLOCKED' if self.closed else None,valid_until=min([now+.5,self.spec.expires_at]+[v for v in self.deadlines.values() if v>now]) if live else now)


class TelemetryBroker(OfflineBroker):
    def __init__(self,spec,api_token,receiver,clock):
        import hmac
        if receiver.spec!=spec or hmac.compare_digest(api_token,receiver.token):raise SecurityError('Independent API/telemetry capabilities required')
        super().__init__(spec,api_token,clock);self.receiver=receiver;self.telemetry=receiver
    def dispatch(self,peer,request):
        value=self.policy.accept(peer,request)
        if value['operation']!='observe':raise SecurityError('Telemetry observation only')
        details=self.receiver.snapshot();base=dict(session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,sequence=details['sequence'],telemetry_source='PX4',telemetry_transport='BROKER',monitor_only=True,connected=details['connected'],fresh=details['fresh'],home=None,position=details['position'],battery=details['battery'],flight_ready=False,flight_authorized=False)
        return self.policy.respond(dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,sequence=value['sequence'],request_id=value['request_id'],issued_at=self.clock(),ttl=.5,operation='observe',data=dict(schema=getattr(self.receiver,'schema','PX4_TELEMETRY_V1'),observation=base,telemetry=details)))


class TelemetryClient(ObservationClient):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.closed=False;self.details=None
    def request(self,operation):
        try:
            if self.closed or operation!='observe':raise SecurityError('Read-only telemetry client unavailable')
            return super().request(operation)
        except Exception:
            self.closed=True;self.details=None
            raise SecurityError('Telemetry client requires explicit fresh-session binding') from None
    def _validate_data(self,operation,data):
        if operation!='observe' or type(data) is not dict or set(data)!={'schema','observation','telemetry'} or data['schema']!='PX4_TELEMETRY_V1':raise SecurityError('Typed telemetry observation required')
        super()._validate_data('observe',data['observation'])
        d=data['telemetry'];b=data['observation']
        if type(d) is not dict or set(d)!={'session_uuid','epoch','sequence','connected','fresh','position','battery','flight_mode','relative_altitude_m','mission','error','valid_until'}:raise SecurityError('Telemetry details rejected')
        for key in ('session_uuid','epoch','sequence','connected','fresh','position','battery'):
            if type(d[key]) is not type(b[key]) or d[key]!=b[key]:raise SecurityError('Telemetry fields disagree')
        if d['flight_mode'] is not None and (type(d['flight_mode']) is not str or len(d['flight_mode'])>24):raise SecurityError('Mode rejected')
        if d['relative_altitude_m'] is not None:finite(d['relative_altitude_m'])
        if d['error'] not in (None,'SOURCE_BLOCKED'):raise SecurityError('Source error rejected')
        if d['fresh'] and not self.clock()<finite(d['valid_until'])<=min(self.clock()+.5,self.spec.expires_at):raise SecurityError('Telemetry projection expired/out of bounds')
        m=d['mission']
        if m is not None:
            if set(m)!={'sequence','total','state','progress','progress_basis'} or m['progress_basis']!='MISSION_ITEM_INDEX':raise SecurityError('Mission details rejected')
            integer(m['sequence'],0,65535);integer(m['state'],0,6)
            if m['total'] is not None:
                integer(m['total'],1,65535)
                if m['sequence']>=m['total']:raise SecurityError('Mission index rejected')
            expected=100 if m['total'] and m['state']==5 else min(99,int(100*m['sequence']/m['total'])) if m['total'] and m['state'] in (2,3,4) else None
            if type(m['progress']) is not type(expected) or m['progress']!=expected:raise SecurityError('Mission progress inconsistent')
            if m['progress'] is not None:integer(m['progress'],0,100)
        if not d['fresh'] and any(d[k] is not None for k in ('flight_mode','relative_altitude_m','mission')):raise SecurityError('Stale details retained')
    def observe(self):
        result=self.request('observe');self.details=copy.deepcopy(result['telemetry']);return result['observation']


class PTelemetryCollector:
    """P-side fixed snapshot callback. No command, key-file or network API.

    Installation must bind UID/kernel identity/current messages in trusted code;
    lambda fixtures do not constitute production source attestation.
    """
    def __init__(self,spec,token,identity_reader,read_messages,clock,uid_reader):
        if type(token) is not bytes or len(token)!=32 or not any(token):raise SecurityError('Telemetry capability required')
        self.spec,self.token,self.identity,self.read,self.clock,self.uid=spec,token,identity_reader,read_messages,clock,uid_reader
        self.sequence=0;self.closed=False
    def collect(self,nonce):
        try:
            if self.closed or self.uid()!=self.spec.px4_uid:raise SecurityError('P collector role required')
            uuid4(nonce);start=finite(self.clock());self.spec.validate_time(start);before=self.identity()
            validate_observed_identity(self.spec,before,before)
            rows=copy.deepcopy(self.read(nonce,start+1))
            end=finite(self.clock())
            if self.identity()!=before or not start<=end<start+1:raise SecurityError('P identity/deadline changed')
            self.sequence+=1
            from dataclasses import asdict
            return seal(self.token,dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,challenge=nonce,sequence=self.sequence,issued_at=start,ttl=1.,identity=asdict(before),messages=rows))
        except Exception:
            self.closed=True;raise SecurityError('P telemetry collector blocked') from None


def prepare_telemetry_server(provider,receiver,api_token):
    """Bind the existing gated IPC transaction layer, without allocating sockets.

    Installed composition must still supply the trusted listener/backend and
    independently authorized P control channel. No CLI/live enable switch.
    """
    from .activation import TrustedActivationProvider
    from .ipc_transport import IPCServerTransport
    import hashlib,hmac
    try:
        from .telemetry_exporter import NativeTelemetryReceiver
        if type(provider) is not TrustedActivationProvider or provider.role!='broker' or type(receiver) not in (TelemetryReceiver,NativeTelemetryReceiver):
            raise SecurityError('Prepared broker provider required')
        if receiver.spec!=provider.spec or receiver.identity!=provider._verifier.expected:
            raise SecurityError('P attestation/telemetry identity mismatch')
        secrets=(provider.trust._secret,provider._verifier._token,api_token)
        if any(hmac.compare_digest(receiver.token,s) for s in secrets) or hashlib.sha256(receiver.token).digest()==provider._fingerprint:
            raise SecurityError('Independent telemetry capability required')
        if any(hmac.compare_digest(api_token,s) for s in secrets[:2]) or hashlib.sha256(api_token).digest()==provider._fingerprint:
            raise SecurityError('Independent observation capability required')
        provider.authorize('listen_ipc')  # UID/code/manifest/ledger/current P proof before server use.
        if type(receiver) is NativeTelemetryReceiver and 'secure_telemetry' not in provider.last_state['modules']:raise SecurityError('Exporter current binary required')
        broker=TelemetryBroker(provider.spec,api_token,receiver,provider.clock)
        return IPCServerTransport(broker,session_claims=provider._lease)
    except Exception:
        if isinstance(receiver,TelemetryReceiver):receiver.close()
        if type(provider) is TrustedActivationProvider:provider.close()
        raise SecurityError('Telemetry service preparation blocked') from None
