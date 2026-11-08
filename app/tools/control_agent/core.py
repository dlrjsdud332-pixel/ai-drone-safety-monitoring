"""Bounded, auditable rule-based triage; detection scores are not probabilities."""
from dataclasses import dataclass,asdict
from collections import deque
import copy
import json
import math
import os
import stat
from threading import RLock
import time
from uuid import uuid4

class EventRejected(ValueError):pass
ALIASES={'NO_HARDHAT':'NO-Hardhat','NO HARDHAT':'NO-Hardhat','NO-HARDHAT':'NO-Hardhat','NO_VEST':'NO-Safety Vest','NO VEST':'NO-Safety Vest','NO_SAFETY_VEST':'NO-Safety Vest','NO-SAFETY VEST':'NO-Safety Vest','INTRUSION':'DANGER ZONE','DANGER_ZONE':'DANGER ZONE'}
RULES={
 'FIRE':('HIGH','화재 의심: 현장 확인·안전 담당자 통보 및 현장 대응 절차 확인을 권고합니다.'),
 'SMOKE':('CAUTION','연기 의심: 위치와 발화 여부 확인·현장 점검을 권고합니다.'),
 'FALL':('HIGH','넘어짐 의심: 작업자 상태 확인 및 현장 응급 대응 절차 확인을 권고합니다.'),
 'NO-Hardhat':('CAUTION','안전모 미착용 의심: 현장 담당자의 착용 상태 점검을 권고합니다.'),
 'NO-Safety Vest':('CAUTION','안전조끼 미착용 의심: 현장 담당자의 착용 상태 점검을 권고합니다.'),
 'DANGER ZONE':('HIGH','위험구역 접근을 확인하고 현장 안전 담당자에게 알림을 권고합니다.'),
 'PERSON':('INFO','사람 탐지 정보입니다. 다른 위험 근거가 없으면 사고로 판단하지 않습니다.')}
RANK={'UNKNOWN':-1,'INFO':0,'CAUTION':1,'HIGH':2}
TRANSITIONS={'PENDING':{'APPROVED','HELD','REJECTED'},'HELD':{'APPROVED','REJECTED'},'APPROVED':set(),'REJECTED':set()}

def number(v):
 if type(v) not in (int,float) or not math.isfinite(v):raise EventRejected('finite number required')
 return v

def label(v,maximum=100):
 if type(v) is not str or not v.strip() or len(v)>maximum or any(ord(c)<32 for c in v):raise EventRejected('invalid label')
 return v

def box(v):
 if not isinstance(v,(tuple,list)) or len(v)!=4:raise EventRejected('image box required')
 a,b,c,d=map(number,v)
 if not 0<=a<c or not 0<=b<d:raise EventRejected('invalid image bounds')
 return (a,b,c,d)

@dataclass(frozen=True)
class DetectionEvent:
 kind:str
 confidence:float|None
 camera_id:str
 occurred_at:float
 source_id:str
 source_kind:str
 valid:bool=False
 track_id:str|None=None
 location:tuple|None=None  # image xyxy; never silently treated as GPS
 zone:tuple|None=None     # image rectangle, no invented geographic position
 evidence_method:str='UNKNOWN'
 def checked(self,now,ttl):
  key=label(self.kind).upper();kind=ALIASES.get(key,key)
  if kind not in RULES:raise EventRejected('unsupported event kind')
  if self.valid is not True:raise EventRejected('invalid detector observation')
  label(self.camera_id,32);label(self.source_id,100)
  if self.source_kind not in ('OFFLINE_DEMO','LOCAL_VIDEO','CAMERA','FIXTURE'):raise EventRejected('unknown provenance')
  if self.evidence_method not in ('DETECTOR_SCORE','HEURISTIC_SCORE','UNKNOWN'):raise EventRejected('unknown score meaning')
  if not 0<=number(now)-number(self.occurred_at)<ttl:raise EventRejected('stale/future detection')
  if self.confidence is not None and not 0<=number(self.confidence)<=1:raise EventRejected('invalid confidence')
  minimum={'FIRE':.35,'SMOKE':.5,'PERSON':.35,'NO-Hardhat':.5,'NO-Safety Vest':.5}.get(kind,0.)
  if self.evidence_method=='DETECTOR_SCORE' and self.confidence is not None and self.confidence<minimum:raise EventRejected('below advisory rule threshold')
  if self.track_id is not None:label(self.track_id,64)
  return DetectionEvent(kind,self.confidence,self.camera_id,self.occurred_at,self.source_id,self.source_kind,True,self.track_id,box(self.location) if self.location is not None else None,box(self.zone) if self.zone is not None else None,self.evidence_method)

class MemoryAudit:
 def __init__(self):self.rows=deque(maxlen=256)
 def append(self,row):self.rows.append(copy.deepcopy(row))

class JsonAudit:
 """Local non-operational incident journal. Never deletes earlier evidence."""
 def __init__(self,path,maximum=4*1024*1024):self.path=os.fspath(path);self.maximum=maximum
 def append(self,row):
  data=(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n').encode()
  fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
  try:
   s=os.fstat(fd)
   if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1 or s.st_size+len(data)>self.maximum:raise OSError('audit permission/capacity')
   if os.write(fd,data)!=len(data):raise OSError('partial audit write')
   os.fsync(fd)
  finally:os.close(fd)

class IncidentAgent:
 """Human states acknowledge incidents only. There is no execution capability."""
 def __init__(self,clock=time.time,*,ttl=3.,audit=None):
  if not 0<number(ttl)<=10:raise ValueError('bounded evidence TTL required')
  self.clock,self.ttl,self.audit=clock,ttl,audit if audit is not None else MemoryAudit()
  self.active={};self.history=deque(maxlen=128);self.audit_status='OK';self.rejected=0;self.duplicates=0;self.counter=0;self.run_id=uuid4().hex[:12];self.lock=RLock();self.camera_sources={};self.last_error=None
  if hasattr(self.audit,'restore'):
   try:self.history.extend(self.audit.restore(self.clock()))
   except Exception:self.audit_status='BLOCKED';self.last_error='HISTORY_RESTORE_FAILED'
 def _log(self,row):
  try:self.audit.append(copy.deepcopy(row));return True
  except Exception:self.audit_status='BLOCKED';return False
 def _end(self,key,reason):
  row=self.active.pop(key);row['state']='HISTORY';row['end_reason']=reason;row['ended_at']=self.clock();self.history.append(row);self._log(dict(action='INCIDENT_ENDED',incident=row))
 def _expire(self):
  now=self.clock()
  for key,row in list(self.active.items()):
   if not 0<=now-row['last_seen']<self.ttl:self._end(key,'EVIDENCE_EXPIRED')
 def bind_camera(self,camera_id,source_id,*,available=True):
  with self.lock:
   old=self.camera_sources.get(camera_id)
   if old!=source_id or not available:
    for key in list(self.active):
     if key[0]==camera_id:self._end(key,'SOURCE_CHANGED' if old!=source_id else 'SOURCE_UNAVAILABLE')
   self.camera_sources[camera_id]=source_id
 def ingest(self,event):
  with self.lock:
   try:
    if type(event) is not DetectionEvent:raise EventRejected('typed event required')
    e=event.checked(self.clock(),self.ttl);self._expire()
    if e.camera_id in self.camera_sources and self.camera_sources[e.camera_id]!=e.source_id:raise EventRejected('inactive camera source')
    if e.camera_id not in self.camera_sources and len(self.camera_sources)>=128:raise EventRejected('camera capacity')
    self.camera_sources.setdefault(e.camera_id,e.source_id)
    key=(e.camera_id,e.source_id,e.kind,e.track_id);row=self.active.get(key)
    evidence=asdict(e)
    if row and e.occurred_at<row['last_seen']:raise EventRejected('out of order')
    if row and e.occurred_at==row['last_seen']:
     if evidence==row['evidence'][-1]:self.duplicates+=1;return row['id']
     raise EventRejected('conflicting same-time evidence')
    severity,recommendation=RULES[e.kind]
    if row is None:
     self.counter+=1;row=dict(id=f'{self.run_id}-INC-{self.counter:06d}',kind=e.kind,camera_id=e.camera_id,source_id=e.source_id,source_kind=e.source_kind,track_id=e.track_id,state='CURRENT',severity=severity,first_seen=e.occurred_at,last_seen=e.occurred_at,observed_duration=0.,count=0,evidence=[],review='PENDING',reviews=[],recommendation=recommendation,reason=f'RULE_V1:{e.kind}; detection/heuristic score is not accident probability; incident unconfirmed',execution_authorized=False,field_action_status='UNKNOWN_NOT_REPORTED',last_audit=e.occurred_at)
     if len(self.active)>=128:
      victim=min(self.active,key=lambda k:(RANK[self.active[k]['severity']],self.active[k]['last_seen']))
      if RANK[severity]<RANK[self.active[victim]['severity']]:raise EventRejected('capacity: lower priority evidence rejected')
      self._end(victim,'CAPACITY_EVICTED')
     self.active[key]=row
    row['last_seen']=e.occurred_at;row['observed_duration']=e.occurred_at-row['first_seen'];row['count']+=1;row['evidence']=(row['evidence']+[evidence])[-8:]
    if row['count']==1 or e.occurred_at-row['last_audit']>=1:
     self._log(dict(action='DETECTION' if row['count']==1 else 'EVIDENCE_UPDATE',incident=row));row['last_audit']=e.occurred_at
    self.last_error=None;return row['id']
   except EventRejected as error:self.rejected+=1;self.last_error=str(error);return None
 def review(self,incident_id,status,operator,note=''):
  with self.lock:
   self._expire();label(operator,64)
   if status not in ('APPROVED','HELD','REJECTED') or type(note) is not str or not note.strip() or len(note)>400:raise ValueError('review reason required')
   rows=list(self.active.values())+list(self.history);row=next((r for r in rows if r['id']==incident_id),None)
   if row is None and hasattr(self.audit,'get'):
    try:row=self.audit.get(incident_id)
    except Exception:self.audit_status='BLOCKED';raise ValueError('history unavailable') from None
    if row is not None:self.history.append(row)
   if row is None:raise ValueError('unknown incident')
   if status not in TRANSITIONS[row['review']]:raise ValueError('duplicate/final review transition')
   review=dict(status=status,operator=operator.strip(),operator_identity='LOCAL_UNAUTHENTICATED_INPUT',at=self.clock(),note=note,execution_authorized=False)
   if self.audit_status!='OK' or not self._log(dict(action='HUMAN_REVIEW',incident_id=incident_id,expected_review=row['review'],observed_snapshot=copy.deepcopy(row),review=review)):raise ValueError('review audit unavailable')
   row['review']=status;row['reviews']=(row['reviews']+[review])[-16:];return copy.deepcopy(review)
 def snapshot(self):
  with self.lock:
   self._expire();rows=sorted(self.active.values(),key=lambda r:(-RANK[r['severity']],-r['last_seen'],r['id']))
   counts={state:sum(r['review']==state for r in list(self.active.values())+list(self.history)) for state in ('PENDING','HELD')}
   if hasattr(self.audit,'unresolved_counts') and self.audit_status=='OK':
    try:counts=self.audit.unresolved_counts()
    except Exception:self.audit_status='BLOCKED'
   return copy.deepcopy(dict(unresolved_counts=counts,mode='ADVISORY_ONLY',current=rows,history=list(self.history),severity=rows[0]['severity'] if rows else 'UNKNOWN',audit_status=self.audit_status,rejected=self.rejected,duplicates=self.duplicates,last_error=self.last_error,flight_commands=0,execution_authorized=False))

 def lookup(self,incident_id):
  with self.lock:
   self._expire()
   row=next((r for r in list(self.active.values())+list(self.history) if r['id']==incident_id),None)
   if row is None and hasattr(self.audit,'get') and self.audit_status=='OK':
    try:row=self.audit.get(incident_id)
    except Exception:self.audit_status='BLOCKED'
   return copy.deepcopy(row)
 def catalog(self,scope='ALL',offset=0,limit=100):
  with self.lock:
   self._expire()
   if hasattr(self.audit,'catalog') and self.audit_status=='OK':
    try:return [self.lookup(r['id']) for r in self.audit.catalog(scope,offset,limit)]
    except Exception:self.audit_status='BLOCKED'
   rows=list(self.active.values())+list(self.history)
   if scope=='CURRENT':rows=[r for r in rows if r['state']=='CURRENT']
   if scope=='HISTORY':rows=[r for r in rows if r['state']=='HISTORY']
   if scope=='UNRESOLVED':rows=[r for r in rows if r['review'] in ('PENDING','HELD')]
   rows.sort(key=lambda r:(-RANK[r['severity']],-r['last_seen']) if scope in ('CURRENT','UNRESOLVED') else (-r['last_seen'],r['id']))
   return copy.deepcopy(rows[offset:offset+limit])
