"""Connect reused incident store/UI to local reports, optical flow and SIM only."""
import time
import threading
from pathlib import Path
from .reports import Reports
from .patrol import SimPatrol
from .flow import SmokeFlow
from tools.data_center import DataCenter
from tools.data_center.adapter import ExistingRecordsAdapter

class Automation:
    def __init__(self,agent,root):
        self.hub=DataCenter(Path(root)/"data_center");self.hub_adapter=ExistingRecordsAdapter(self.hub);self.hub_status="OK"
        self.agent=agent;self.reports=Reports(root);self.flow=SmokeFlow();self.patrol=SimPatrol(self.reports.patrol);self.last=0;self.error=None;self.frames={};self.sequence={};self.last_patrol_save=0;self.flight=None;self.sync_thread=None
    def observe_patrol(self,channels):
        if self.flight is not None:return  # Existing explicit PX4 fixture scenario is separate.
        now=time.monotonic();sources={};expires=[]
        try:
            if hasattr(self,'event_bridge'):self.event_bridge.sync_channels(channels)
            observation=self.observation_provider();details=observation.get('telemetry') or {}
            until=details.get('valid_until', (observation.get('last_good') or 0)+2)
            if not observation.get('fresh') or observation.get('error'):until=0.
            snapshot=self.agent.snapshot()
            if snapshot['audit_status']!='OK':until=0.
            with self.agent.lock:bindings=dict(self.agent.camera_sources)
            for c in channels:
                with c.lock:
                    if not c.source:continue
                    camera=str(c.number);sources[camera]=(id(c),c.source,getattr(c,'source_generation',0),bindings.get(camera))
                    expires.append(c.received_at+2 if c.status=='Playing' and 0<=now-c.received_at<2 and not c.stop_event.is_set() else 0.)
            self.patrol.observe(self.patrol.cycle,snapshot['current'],sources,min([until]+expires) if expires else 0.,event_ttl=self.agent.ttl)
        except Exception:
            self.patrol.observe(self.patrol.cycle,[],{},0.)
    def update(self,channels):
        self.observe_patrol(channels)
        if self.flight is not None:
            try:self.flight.tick()
            except Exception:
                self.flight.adapter.sample=None;self.flight.adapter.state="ABORTED";self.flight.adapter.reason="OFFLINE_SCENARIO_ERROR"
        try:self.patrol.tick()
        except Exception:self.error="PATROL_HISTORY_BLOCKED"
        for c in channels:
            camera=str(c.number)
            with c.lock:
                available=bool(c.source) and c.status=='Playing' and 0<=time.monotonic()-c.received_at<3
                seq=c.analysis_sequence;source=(id(c),c.source);capture=getattr(c,'automation_capture',None)
                new=self.sequence.get(camera)!=(source,seq)
                frame=capture[2] if new and capture is not None and capture[0]==c.source else None
                # Analyze raw source frames, not annotated/zoomed UI pixels.
                smoke=capture[3] if capture else None
            if not available:self.flow.clear(camera);self.frames.pop(camera,None);self.sequence.pop(camera,None);continue
            if frame is not None:self.frames[camera]=frame
            if not new:continue
            self.sequence[camera]=(source,seq);box=None;largest=0
            if smoke is not None:
                for b in smoke.boxes:
                    name=str(smoke.names[int(b.cls[0])]).lower()
                    if 'smoke' in name and float(b.conf[0])>=getattr(c,'smoke_conf_threshold',.5):
                        coords=tuple(float(x) for x in b.xyxy[0].tolist());area=(coords[2]-coords[0])*(coords[3]-coords[1])
                        if area>largest:box=coords;largest=area
            if frame is not None:
                at=time.time()-(time.monotonic()-capture[1])
                try:self.flow.update(camera,source,frame,box,at)
                except Exception:self.flow.clear(camera)
        if time.monotonic()-self.last>=1:
            self.last=time.monotonic()
            if self.sync_thread is None or not self.sync_thread.is_alive():
                self.sync_thread=threading.Thread(target=self.sync,name='offline-report-sync',daemon=True)
                self.sync_thread.start()
        if time.monotonic()-self.last_patrol_save>=5 and self.patrol.state in ('PATROLLING','RETURNING'):
            self.last_patrol_save=time.monotonic()
            try:self.reports.patrol(dict(self.patrol.snapshot(),at=time.time()))
            except Exception:self.patrol.state='ABORTED';self.patrol.reason='HISTORY_WRITE_FAILED';self.error='PATROL_HISTORY_BLOCKED'
    def sync(self):
        try:
            self.hub_status='OK'
            self.hub.prune_telemetry()
            if self.flight is not None:self.hub_adapter.flight(self.flight.snapshot(),self.flight.plan)
        except Exception:self.hub_status='BLOCKED'
        try:
            for offset in range(0,5000,100):
                rows=self.agent.catalog('ALL',offset,100)
                for row in rows:
                    flow=self.flow.get(row['camera_id']) if row['state']=='CURRENT' and row['kind']=='SMOKE' else None
                    old=self.reports.get(row['id'])
                    if flow is None and old is not None:flow=old['smoke_flow']
                    self.reports.update(row,flow,self.frames.get(row['camera_id']) if row['state']=='CURRENT' else None,self.patrol.snapshot(),self.flight.adapter.context_at(row['camera_id'],row['first_seen']) if self.flight is not None and row['state']=='CURRENT' else None)
                    try:self.hub_adapter.report(self.reports.get(row['id']))
                    except Exception:self.hub_status='BLOCKED'
                if len(rows)<100:break
            if self.agent.snapshot()['audit_status']!='OK':raise ValueError('authoritative audit blocked')
            self.error=None
        except Exception as e:self.error='REPORT_BLOCKED:'+type(e).__name__;self.reports.status='BLOCKED'
    def close(self):
        if self.sync_thread is not None:self.sync_thread.join()
        if self.patrol.state in ('PATROLLING','PAUSED','RETURNING'):
            try:self.patrol.abort()
            except Exception:pass
        self.sync();self.reports.close();self.hub.close()
