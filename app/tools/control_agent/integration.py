"""Legacy event tap in a separately loaded module; no tracker source edits."""
from threading import RLock
import time
from uuid import uuid4
from .core import DetectionEvent,ALIASES,RULES

class LegacyEvidenceBridge:
 def __init__(self,agent,mission_context=lambda:None):self.agent=agent;self.mission_context=mission_context;self.lock=RLock();self.channels={};self.sources={};self.predict_times={};self.errors=0
 def sync_channels(self,channels):
  with self.lock:
   new={str(c.number):c for c in channels}
   for camera in self.channels.keys()-new.keys():
    if camera in self.sources:self.agent.bind_camera(camera,self.sources[camera][1],available=False)
   self.channels=new
   for c in channels:self._source(c)
 def _source(self,c):
  camera=str(c.number)
  if self.channels.get(camera) is not c:return None
  with c.lock:
   identity=(id(c),c.source,getattr(c,'source_generation',0),self.mission_context());available=(not hasattr(c,'analysis_is_current') or c.analysis_is_current()) and bool(c.source) and c.status=='Playing' and 0<=time.monotonic()-c.received_at<self.agent.ttl
  previous=self.sources.get(camera)
  if previous is None or previous[0]!=identity or (available and not previous[2]):
   source=str(uuid4());self.predict_times.pop(camera,None)
  else:source=previous[1]
  self.sources[camera]=(identity,source,available);self.agent.bind_camera(camera,source,available=available)
  return source if available else None
 def _capture(self,c):
  raw=getattr(c,'processing_received_at',getattr(c,'analysis_received_at',0.))
  age=time.monotonic()-raw
  return time.time()-age if raw>0 and 0<=age<self.agent.ttl else None
 def event(self,c,kind,confidence,track_id=None):
  with self.lock:
   if getattr(c,'processing_mission_context',self.mission_context())!=self.mission_context():return None
   source=self._source(c);stamp=self._capture(c);key=ALIASES.get(str(kind).upper(),str(kind).upper())
   if source is None or stamp is None or key not in RULES or type(confidence) is bool:return None
   group='fire' if key in ('FIRE','SMOKE') else 'ppe' if key in ('NO-Hardhat','NO-Safety Vest') else 'pose' if key=='FALL' else None
   if group is not None:stamp=min(stamp,self.predict_times.get(str(c.number),{}).get(group,stamp))
   with c.lock:zone=tuple(c.danger) if getattr(c,'danger',None) is not None else None
   method='UNKNOWN' if confidence is None else 'DETECTOR_SCORE' if key in ('FIRE','SMOKE','PERSON') else 'HEURISTIC_SCORE'
   return self.agent.ingest(DetectionEvent(key,None if confidence is None else float(confidence),str(c.number),stamp,source,'OFFLINE_DEMO',valid=True,track_id=str(track_id) if track_id is not None else None,zone=zone,evidence_method=method))
 def persons(self,c,result):
  with self.lock:
   if getattr(c,'processing_mission_context',self.mission_context())!=self.mission_context():return
   source=self._source(c);stamp=self._capture(c)
   if source is None or stamp is None:return
   for b in result.boxes:
    if int(b.cls[0])!=0:continue
    track=str(int(b.id[0])) if b.id is not None else None
    self.agent.ingest(DetectionEvent('PERSON',float(b.conf[0]),str(c.number),stamp,source,'OFFLINE_DEMO',valid=True,track_id=track,location=tuple(float(x) for x in b.xyxy[0].tolist()),evidence_method='DETECTOR_SCORE'))
 def failed(self):self.errors+=1


def attach(app,agent,*,mission_context=lambda:None):
 """Install only on the integration's private loaded legacy module."""
 bridge=LegacyEvidenceBridge(agent,mission_context);base_event=app.EventManager;base_analyzer=app.Analyzer;missing=object()
 class Events(base_event):
  def emit(self,channel,kind,confidence=missing,track_id=None,details=None,frame=None):
   if getattr(channel,'processing_mission_context',bridge.mission_context())!=bridge.mission_context():return False
   linked=dict(details or {})
   try:
    identity=bridge.event(channel,kind,None if confidence is missing else confidence,track_id)
    row=agent.lookup(identity) if identity else None
    if row is not None:
     linked.update(incident_id=identity,incident_source_id=row['source_id'],incident_camera_id=row['camera_id'],
                   observed_at=row['last_seen'],frame_sequence=getattr(channel,'processing_sequence',None),
                   incident_audit_status=agent.snapshot()['audit_status'])
   except Exception:bridge.failed()
   if confidence is missing:return super().emit(channel,kind,track_id=track_id,details=linked,frame=frame)
   return super().emit(channel,kind,confidence,track_id,linked,frame)
 class Analyzer(base_analyzer):
  def process(self,c,model,pose,ppe,fire_smoke,frame):
   c.processing_mission_context=bridge.mission_context()
   seen=[]
   class TrackTap:
    def track(self,*a,**kw):
     value=model.track(*a,**kw);seen.extend(value);return value
    def __getattr__(self,name):return getattr(model,name)
   class PredictTap:
    def __init__(self,wrapped,group):self.wrapped,self.group=wrapped,group
    def predict(self,*a,**kw):
     value=self.wrapped.predict(*a,**kw)
     with bridge.lock:
      if bridge.channels.get(str(c.number)) is c and (not hasattr(c,'analysis_is_current') or c.analysis_is_current()):bridge.predict_times.setdefault(str(c.number),{})[self.group]=bridge._capture(c) or 0.
     return value
    def __getattr__(self,name):return getattr(self.wrapped,name)
   result=super().process(c,TrackTap(),PredictTap(pose,'pose'),PredictTap(ppe,'ppe') if ppe is not None else None,PredictTap(fire_smoke,'fire') if fire_smoke is not None else None,frame)
   if hasattr(c,'analysis_is_current') and not c.analysis_is_current():return result
   # Exact analyzed raw frame/time, independent of display zoom or later reader frames.
   if frame is not None:
    with c.lock:c.automation_capture=(c.source,getattr(c,'processing_received_at',0.),frame.copy(),getattr(c,'fire_result',None))
   try:
    for detections in seen:bridge.persons(c,detections)
   except Exception:bridge.failed()
   return result
 app.EventManager,app.Analyzer=Events,Analyzer
 return bridge
