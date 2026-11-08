"""Native uORB exporter DTO + fixed protected-file collector, no socket defaults."""
import copy
import math
from dataclasses import asdict
from pathlib import PurePosixPath
from .spec import SecurityError,finite,integer,uuid4,strict_json
from .platform_macos import checked_file
from .access import validate_private_chain
from .session import validate_observed_identity
from .policy import seal,unseal
from .telemetry_feed import TelemetryReceiver,TelemetryClient

SCHEMA='PX4_NATIVE_TELEMETRY_V1'
TOPICS={
 'sensor_gnss':{'timestamp','timestamp_sample','lat','lon','alt_msl','fix_type'},
 'vehicle_global_position':{'timestamp','timestamp_sample','lat','lon','alt_msl','lat_lon_valid','alt_valid'},
 'home_position':{'timestamp','alt_msl','valid_alt'},
 'battery_status':{'timestamp','connected','remaining'},
 'vehicle_status':{'timestamp','nav_state'},
 'mission_result':{'timestamp','valid','finished','failure','seq_current','seq_total'}}
NAV={0:'MANUAL',1:'ALTCTL',2:'POSCTL',3:'MISSION',4:'LOITER',5:'RTL',10:'ACRO',14:'OFFBOARD',15:'STABILIZED',17:'TAKEOFF',18:'LAND',19:'FOLLOW_TARGET',20:'PRECLAND',21:'ORBIT'}


def validate_export(spec,value,identity,nonce,now):
    if type(value) is not dict or set(value) not in ({'schema','nonce','session_uuid','epoch','pid','uid','host_us','hrt_us','topics'},{'schema','nonce','session_uuid','epoch','pid','uid','host_us','hrt_us','topics','data_origin'},{'schema','nonce','session_uuid','epoch','pid','uid','host_us','hrt_us','topics','data_origin','source_id'}) or value['schema']!='PX4_UORB_EXPORT_V1':raise SecurityError('Native export schema rejected')
    if (value['nonce'],value['session_uuid'],value['epoch'])!=(nonce,spec.session_uuid,spec.epoch):raise SecurityError('Native export session/nonce mismatch')
    for key in ('pid','uid','host_us','hrt_us'):integer(value[key],1,2**63-1)
    if (value['pid'],value['uid'])!=(identity.pid,identity.uid):raise SecurityError('Native export owner changed')
    if value.get('data_origin','UNKNOWN') not in ('UNKNOWN','PHYSICAL_SENSOR','SIMULATOR','OFFLINE_DEMO'):raise SecurityError('Source origin rejected')
    if value.get('source_id') is not None:uuid4(value['source_id'])
    host=value['host_us']/1e6;hrt=value['hrt_us'];now=finite(now)
    if not 0<=now-host<1:raise SecurityError('Native export clock/age unverified')
    topics=value['topics']
    if type(topics) is not dict or set(topics)!=set(TOPICS):raise SecurityError('Complete native topic availability required')
    result={}
    for name,fields in TOPICS.items():
        row=topics[name]
        if row is None:result[name]=None;continue
        if type(row) is not dict or set(row)!=fields:raise SecurityError('Native topic schema rejected')
        integer(row['timestamp'],1,2**63-1)
        if 'timestamp_sample' in row:integer(row['timestamp_sample'],0,2**63-1)
        sample=row.get('timestamp_sample',row['timestamp'])
        if sample==0:result[name]=None;continue
        if not sample<=row['timestamp']<=hrt:raise SecurityError('Native timestamp order rejected')
        for key,v in row.items():
            if key in ('lat_lon_valid','alt_valid','valid_alt','connected','valid','finished','failure'):
                if type(v) is not bool:raise SecurityError('Native validity flags rejected')
            elif key not in ('timestamp','timestamp_sample'):finite(v)
        if 'lat' in row and (not -90<=finite(row['lat'])<=90 or not -180<=finite(row['lon'])<=180):raise SecurityError('Native coordinates rejected')
        if name=='sensor_gnss':integer(row['fix_type'],0,8)
        if name=='battery_status' and not 0<=row['remaining']<=1:raise SecurityError('Native battery rejected')
        if name=='vehicle_status':integer(row['nav_state'],0,255)
        if name=='mission_result':integer(row['seq_current'],0,65535);integer(row['seq_total'],0,65535)
        captured=host-(hrt-sample)/1e6
        result[name]=dict(copy.deepcopy(row),captured_at=captured) if 0<=now-captured<1 else None
    return result


class FixedExporterReader:
    """Execute one approved read-only alias, then read nonce-specific result.

    No default executor/gate. gate() must revalidate P activation/current child;
    the endpoint service below supplies it. Return code/stdout alone never PASS.
    executor is an installed bounded subprocess adapter, injected offline here.
    """
    def __init__(self,spec,identity,binary,metadata,executor,gate,clock,uid_reader):
        self.spec,self.identity,self.binary,self.metadata,self.executor,self.gate,self.clock,self.uid=spec,identity,binary,metadata,executor,gate,clock,uid_reader
        self.closed=False
    def __call__(self,nonce,deadline):
        try:
            if self.closed or self.uid()!=self.spec.px4_uid or self.executor is None or self.gate is None:raise SecurityError('Exporter execution unbound')
            from .export_channel import ExportChannelGate
            owner=getattr(self.gate,'__self__',None)
            if type(owner) is not ExportChannelGate or owner.context.role!='px4' or owner.spec!=self.spec:raise SecurityError('P exporter gate binding required')
            if self.binary!=owner.supervisor._approved_binary:raise SecurityError('Exporter image binding rejected')
            uuid4(nonce);self.gate('export_snapshot');start=finite(self.clock());self.spec.validate_time(start)
            if not start<deadline<=start+1:raise SecurityError('Exporter transaction deadline rejected')
            if not self.binary.startswith('/Library/Application Support/AIDroneSecure/') or not self.binary.endswith('/bin/px4'):raise SecurityError('Installed exporter binary required')
            before=self.identity();validate_observed_identity(self.spec,owner.supervisor.expected,before)
            path=self.spec.runtime_dir+'/export_'+nonce+'.json'
            try:self.metadata(path)
            except FileNotFoundError:pass
            else:raise SecurityError('Reused snapshot file rejected')
            argv=[str(PurePosixPath(self.binary).with_name('px4-secure_telemetry')),'--instance',str(self.spec.instance_id),'snapshot',nonce]
            outcome=self.executor(argv,executable=self.binary,timeout=deadline-start,shell=False)
            if outcome.returncode!=0:raise SecurityError('PX4 exporter failed')
            if self.identity()!=before or not start<=self.clock()<deadline:raise SecurityError('Exporter process/deadline changed')
            validate_private_chain(path,self.metadata,self.spec.px4_uid,leaf_kind='file',leaf_mode=0o600)
            raw=checked_file(path,self.spec.px4_uid,mode=0o600,maximum=4096)
            value=strict_json(raw,limit=4096);validate_export(self.spec,value,before,nonce,self.clock())
            if value['host_us']/1e6<start:raise SecurityError('Snapshot predates query')
            self.gate('export_snapshot')
            return value
        except Exception:
            self.closed=True;raise SecurityError('Current PX4 export unavailable') from None


class NativeExportCollector:
    def __init__(self,spec,token,identity,reader,clock,uid_reader):
        if type(token) is not bytes or len(token)!=32 or not any(token):raise SecurityError('Telemetry capability required')
        self.spec,self.token,self.identity,self.reader,self.clock,self.uid=spec,token,identity,reader,clock,uid_reader
        self.sequence=0;self.closed=False
    def collect(self,nonce,deadline=None):
        try:
            if self.closed or self.uid()!=self.spec.px4_uid or self.sequence>=128:raise SecurityError('P exporter unavailable/budget exhausted')
            uuid4(nonce);start=finite(self.clock());self.spec.validate_time(start);before=self.identity()
            validate_observed_identity(self.spec,before,before)
            deadline=start+1 if deadline is None else finite(deadline)
            if not start<deadline<=start+1:raise SecurityError('Native transaction deadline rejected')
            value=self.reader(nonce,deadline);end=finite(self.clock());self.spec.validate_time(end)
            if self.identity()!=before or not start<=end<deadline:raise SecurityError('Native collection changed identity/deadline')
            validate_export(self.spec,value,before,nonce,end);self.sequence+=1
            return seal(self.token,dict(version=1,session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,challenge=nonce,sequence=self.sequence,issued_at=start,ttl=1.,identity=asdict(before),native=value))
        except Exception:
            self.closed=True;raise SecurityError('Native collector blocked') from None


class NativeTelemetryReceiver(TelemetryReceiver):
    schema=SCHEMA
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw);self.origin='UNKNOWN';self.source_id=None;self.native=None;self.until=0.;self.last_clocks=None
    def close(self):
        super().close();self.native=None;self.until=0.
    def accept(self,peer,raw,nonce):
        try:
            from .platform_macos import require_peer
            from .session import OwnedIdentity
            if self.closed:raise SecurityError('Native receiver closed')
            require_peer(peer,self.spec.px4_uid);uuid4(nonce);now=finite(self.clock());self.spec.validate_time(now);v=unseal(self.token,raw)
            if set(v)!={'version','session_uuid','epoch','challenge','sequence','issued_at','ttl','identity','native'} or type(v['version']) is not int or v['version']!=1:raise SecurityError('Native envelope rejected')
            if (v['session_uuid'],v['epoch'],v['challenge'])!=(self.spec.session_uuid,self.spec.epoch,nonce):raise SecurityError('Native envelope correlation rejected')
            validate_observed_identity(self.spec,self.identity,OwnedIdentity(**v['identity']))
            issued=finite(v['issued_at']);ttl=finite(v['ttl']);seq=integer(v['sequence'],1,128)
            if seq!=self.sequence+1 or not 0<ttl<=1 or not self.spec.issued_at<=issued<=now<issued+ttl or (self.sequence and now>=self.until):raise SecurityError('Native replay/gap rejected')
            native=v['native'];topics=validate_export(self.spec,native,self.identity,nonce,now)
            clocks=(native['host_us'],native['hrt_us'])
            if self.last_clocks is not None:
                dh,dp=clocks[0]-self.last_clocks[0],clocks[1]-self.last_clocks[1]
                if dh<0 or dp<0 or abs(dh-dp)>200000:raise SecurityError('PX4 clock stalled/regressed')
            self.origin=native.get('data_origin','UNKNOWN');self.source_id=native.get('source_id')
            if self.origin!='UNKNOWN' and self.source_id is None:raise SecurityError('Unbound sensor source identity')
            self.native=topics;self.last_clocks=clocks;self.until=min(issued+ttl,self.spec.expires_at);self.sequence=seq
        except Exception:
            self.close();raise SecurityError('Native telemetry blocked') from None
    def snapshot(self):
        now=finite(self.clock());fresh=not self.closed and self.native is not None and self.spec.issued_at<=now<self.until
        if not fresh and self.sequence:self.close()
        topics=self.native or {}
        def current(name):
            r=topics.get(name)
            return r if fresh and r and 0<=now-r['captured_at']<1 else None
        global_=current('vehicle_global_position');home=current('home_position');gnss=current('sensor_gnss');status=current('vehicle_status');battery=current('battery_status');mission=current('mission_result')
        position=None;position_source=None
        if global_ and global_['lat_lon_valid'] and global_['alt_valid']:
            position=dict(lat=global_['lat'],lon=global_['lon'],alt_msl=global_['alt_msl']);position_source='vehicle_global_position'
        elif gnss and gnss['fix_type']>=3:
            position=dict(lat=gnss['lat'],lon=gnss['lon'],alt_msl=gnss['alt_msl']);position_source='sensor_gnss'
        # Preserve raw GNSS versus estimated global provenance, never invent fix.
        altitude=position['alt_msl'] if position else global_['alt_msl'] if global_ and global_['alt_valid'] else None
        relative=altitude-home['alt_msl'] if altitude is not None and home and home['valid_alt'] else None
        m=None
        if mission and mission['valid'] and mission['seq_total']>0 and mission['seq_current']<mission['seq_total']:
            state=6 if mission['failure'] else 5 if mission['finished'] else 3 if status and status['nav_state']==3 and not mission['failure'] else 0
            m=dict(sequence=mission['seq_current'],total=mission['seq_total'],state=state,progress=100 if state==5 else min(99,int(100*mission['seq_current']/mission['seq_total'])) if state==3 else None,progress_basis='MISSION_ITEM_INDEX')
        connected=bool(fresh and any((position,altitude is not None,home and home['valid_alt'],battery and battery['connected'],status,m)))
        if not connected:position=None;altitude=None;relative=None;m=None;battery=None
        expiries=[r['captured_at']+1 for r in topics.values() if r and now<r['captured_at']+1]
        return dict(data_source_id=self.source_id if connected else None,data_origin=self.origin if connected else 'UNKNOWN',session_uuid=self.spec.session_uuid,epoch=self.spec.epoch,sequence=self.sequence,connected=connected,fresh=fresh,position=position,battery=battery['remaining']*100 if battery and battery['connected'] else None,flight_mode=NAV.get(status['nav_state'],'UNKNOWN') if connected and status else None,relative_altitude_m=relative,mission=m,error='SOURCE_BLOCKED' if self.closed else None,valid_until=min([now+.5,self.until]+expiries) if fresh else now,altitude_msl_m=altitude,position_source=position_source if position else None,home_status=dict(altitude_msl_m=home['alt_msl'],captured_at=home['captured_at'],position=None) if connected and home and home['valid_alt'] else None,source_times={name:r['captured_at'] for name,r in topics.items() if connected and current(name)})


class NativeTelemetryClient(TelemetryClient):
    def _validate_data(self,operation,data):
        if type(data) is not dict or data.get('schema')!=SCHEMA:raise SecurityError('Native wire schema required')
        d=data.get('telemetry')
        if type(d) is not dict or not {'altitude_msl_m','position_source','home_status','source_times','data_origin','data_source_id'}<=set(d):raise SecurityError('Native details missing')
        reduced=copy.deepcopy(data);reduced['schema']='PX4_TELEMETRY_V1'
        for k in ('altitude_msl_m','position_source','home_status','source_times','data_origin','data_source_id'):reduced['telemetry'].pop(k)
        super()._validate_data(operation,reduced)
        if d['position_source'] not in (None,'vehicle_global_position','sensor_gnss'):raise SecurityError('Position provenance rejected')
        if d['altitude_msl_m'] is not None:finite(d['altitude_msl_m'])
        if bool(d['position_source'])!=bool(d['position']):raise SecurityError('Position provenance mismatch')
        if d['position'] is not None and d['position']['alt_msl']!=d['altitude_msl_m']:raise SecurityError('MSL fields disagree')
        if not d['connected'] and (d['altitude_msl_m'] is not None or d['position_source'] is not None):raise SecurityError('Native unknown retained data')

        if d['data_origin'] not in ('UNKNOWN','PHYSICAL_SENSOR','SIMULATOR','OFFLINE_DEMO'):raise SecurityError('Telemetry source provenance rejected')
        if d['data_source_id'] is not None:uuid4(d['data_source_id'])
        if d['data_origin']!='UNKNOWN' and d['data_source_id'] is None:raise SecurityError('Unbound telemetry source identity')
        h=d['home_status'];times=d['source_times'];now=finite(self.clock())
        if type(times) is not dict or not set(times)<=set(TOPICS):raise SecurityError('Topic provenance rejected')
        for stamp in times.values():
            if not 0<=now-finite(stamp)<1:raise SecurityError('Expired topic provenance')
        if h is not None:
            if type(h) is not dict or set(h)!={'altitude_msl_m','captured_at','position'} or h['position'] is not None:raise SecurityError('HOME contract rejected')
            finite(h['altitude_msl_m'])
            if h['captured_at']!=times.get('home_position'):raise SecurityError('HOME provenance mismatch')
            if d['relative_altitude_m'] is not None and (d['altitude_msl_m'] is None or abs(d['altitude_msl_m']-h['altitude_msl_m']-d['relative_altitude_m'])>1e-6):raise SecurityError('HOME altitude mismatch')
        elif d['relative_altitude_m'] is not None:raise SecurityError('Relative altitude lacks HOME')
        if not d['connected'] and (h is not None or times):raise SecurityError('Disconnected source retained data')
