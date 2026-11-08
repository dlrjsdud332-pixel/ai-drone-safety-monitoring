"""Project-local OFFLINE_DEMO incident transactions, not a PX4/security ledger.

V1 JSONL payloads are reused as audit records; SQLite adds atomic compare/update,
restart recovery, pagination and duplicate-review rejection. No network/service.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from threading import RLock
from .core import RULES,DetectionEvent,label,number,TRANSITIONS

class StoreBlocked(ValueError):pass


def validate_row(row):
 if type(row) is not dict:raise StoreBlocked('incident schema')
 allowed={'id','kind','camera_id','source_id','source_kind','track_id','state','severity','first_seen','last_seen','observed_duration','count','evidence','review','reviews','recommendation','reason','execution_authorized','last_audit','ended_at','end_reason','field_action_status'}
 if set(row)-allowed:raise StoreBlocked('unknown incident fields')
 if row.get('field_action_status','UNKNOWN_NOT_REPORTED')!='UNKNOWN_NOT_REPORTED':raise StoreBlocked('unverified field action claim')
 for k in ('id','camera_id','source_id'):label(row[k],100)
 if row['kind'] not in RULES or row['severity']!=RULES[row['kind']][0] or row['source_kind'] not in ('OFFLINE_DEMO','FIXTURE','LOCAL_VIDEO'):raise StoreBlocked('offline source/policy')
 if row['execution_authorized'] is not False or row['state'] not in ('CURRENT','HISTORY') or row['review'] not in TRANSITIONS:raise StoreBlocked('incident state')
 if not number(row['first_seen'])<=number(row['last_seen']) or number(row['observed_duration'])!=row['last_seen']-row['first_seen']:raise StoreBlocked('incident time')
 if type(row['count']) is not int or row['count']<1 or type(row['evidence']) is not list or not 1<=len(row['evidence'])<=8:raise StoreBlocked('evidence bound')
 for e in row['evidence']:
  v=DetectionEvent(**e).checked(e['occurred_at'],10)
  if (v.kind,v.camera_id,v.source_id,v.source_kind,v.track_id)!=(row['kind'],row['camera_id'],row['source_id'],row['source_kind'],row['track_id']):raise StoreBlocked('evidence identity')
 label(row['reason'],1000);label(row['recommendation'],500)
 old={'FIRE':'화재 의심: 관리자 확인 및 현장 화재 대응 절차 확인을 권고합니다.','SMOKE':'연기 의심 위치를 확인하고 화재 여부 점검을 권고합니다.'}
 if row['recommendation'] not in (RULES[row['kind']][1],old.get(row['kind'])) or row['reason']!=f"RULE_V1:{row['kind']}; detection/heuristic score is not accident probability; incident unconfirmed":raise StoreBlocked('unrecognized rule recommendation')
 if type(row['reviews']) is not list or len(row['reviews'])>16:raise StoreBlocked('review history bound')
 for review in row['reviews']:
  if set(review)!={'status','operator','operator_identity','at','note','execution_authorized'} or review['status'] not in TRANSITIONS or review['operator_identity']!='LOCAL_UNAUTHENTICATED_INPUT' or review['execution_authorized'] is not False:raise StoreBlocked('review history schema')
  label(review['operator'],64);number(review['at'])
  if type(review['note']) is not str or len(review['note'])>400:raise StoreBlocked('review note bound')
 return copy.deepcopy(row)


class BlockedAudit:
 def __init__(self,reason):self.reason=reason
 def append(self,row):raise StoreBlocked(self.reason)
 def restore(self,now):raise StoreBlocked(self.reason)
 def close(self):pass


class IncidentStore:
 """One app writer per file; all operator changes commit before UI success."""
 def __init__(self,path,maximum=64*1024*1024):
  self.path=Path(path);self.maximum=maximum;self.lock=RLock();self.closed=False;self.blocked=False;self.db=None;self.lockfd=None
  self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  if self.path.is_symlink() or os.path.lexists(self.path.with_suffix('.blocked')):raise StoreBlocked('store blocked/symlink; preserve for diagnosis')
  try:
   lockpath=str(self.path)+'.lock';self.lockfd=os.open(lockpath,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600);self._node(self.lockfd)
   fcntl.flock(self.lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
   try:self._node(fd);s=os.fstat(fd);self.identity=(s.st_ino,s.st_dev)
   finally:os.close(fd)
   self.db=sqlite3.connect(self.path,timeout=.25,isolation_level=None,check_same_thread=False)
   self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA foreign_keys=ON')
   page_size=self.db.execute('PRAGMA page_size').fetchone()[0]
   self.db.execute('PRAGMA max_page_count='+str(max(1,int(self.maximum)//page_size)))
   if self.db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise StoreBlocked('corrupt store')
   self.db.executescript('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,payload TEXT NOT NULL,review TEXT NOT NULL,state TEXT NOT NULL,severity TEXT NOT NULL,seen REAL NOT NULL); CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,uid TEXT UNIQUE NOT NULL,payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS imports(sha TEXT PRIMARY KEY);')
   meta=dict(self.db.execute('SELECT key,value FROM meta'))
   if not meta:
    self.db.execute('INSERT INTO meta VALUES(?,?)',('schema','1'));self.db.execute('INSERT INTO meta VALUES(?,?)',('mode','OFFLINE_DEMO'))
   elif meta!={'schema':'1','mode':'OFFLINE_DEMO'}:raise StoreBlocked('store mode/schema mismatch')
   self._check()
  except Exception:
   self.close();raise StoreBlocked('incident store unavailable; no automatic reset') from None
 @staticmethod
 def _node(fd):
  s=os.fstat(fd)
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1:raise StoreBlocked('private regular file required')
 def _check(self):
  if self.closed or self.blocked:raise StoreBlocked('store unavailable')
  s=os.lstat(self.path)
  if not stat.S_ISREG(s.st_mode) or (s.st_ino,s.st_dev)!=self.identity or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1 or s.st_size>self.maximum:raise StoreBlocked('store identity/capacity')
 def _commit(self):self.db.execute('COMMIT')
 def _transaction(self,work):
  with self.lock:
   self._check()
   self.db.execute('BEGIN IMMEDIATE')
   try:
    result=work();self._commit();return result
   except Exception as error:
    if self.db.in_transaction:self.db.execute('ROLLBACK')
    if isinstance(error,(ValueError,KeyError,TypeError)):raise StoreBlocked('invalid/stale transaction rejected') from None
    self.blocked=True
    # An uncertain I/O outcome must never silently restore as an acknowledged
    # success. Keep the file; a new app refuses it until diagnosed.
    try:
     fd=os.open(self.path.with_suffix('.blocked'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
     try:os.write(fd,b'Uncertain transaction: preserve database and journal; no automatic repair.\n');os.fsync(fd)
     finally:os.close(fd)
    except OSError:pass
    raise StoreBlocked('transaction failed; state not acknowledged') from None
 def _write(self,row,*,legacy=False):
  action=row.get('action');raw=json.dumps(row,ensure_ascii=False,allow_nan=False,sort_keys=True)
  if len(raw.encode())>65536:raise StoreBlocked('record bound')
  uid=hashlib.sha256(raw.encode()).hexdigest()
  if self.db.execute('SELECT 1 FROM audit WHERE uid=?',(uid,)).fetchone():return
  if action in ('DETECTION','EVIDENCE_UPDATE','INCIDENT_ENDED'):
   if set(row)!={'action','incident'}:raise StoreBlocked('audit record schema')
   r=validate_row(row['incident']);old=self.db.execute('SELECT payload,review FROM incidents WHERE id=?',(r['id'],)).fetchone()
   if old:
    before=json.loads(old[0])
    if any(before[k]!=r[k] for k in ('camera_id','source_id','kind','track_id','first_seen')):raise StoreBlocked('incident identity mutation')
    r['review']=old[1];r['reviews']=before.get('reviews',[])
   else:
    if action!='DETECTION' or r['review']!='PENDING':raise StoreBlocked('new incident must start pending')
    r['review']='PENDING';r['reviews']=[]
   self.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,severity=excluded.severity,seen=excluded.seen', (r['id'],json.dumps(r,ensure_ascii=False,allow_nan=False),r['review'],r['state'],r['severity'],r['last_seen']))
  elif action=='HUMAN_REVIEW':
   if not {'action','incident_id','review'}<=set(row) or set(row)-{'action','incident_id','review','expected_review','observed_snapshot'}:raise StoreBlocked('review record schema')
   found=self.db.execute('SELECT payload,review FROM incidents WHERE id=?',(row['incident_id'],)).fetchone()
   if not found:raise StoreBlocked('unknown review incident')
   r=json.loads(found[0])
   if 'observed_snapshot' in row:
    observed=validate_row(row['observed_snapshot'])
    if any(observed[k]!=r[k] for k in ('id','camera_id','source_id','kind','track_id','first_seen','state')) or observed['review']!=found[1] or observed['last_seen']<r['last_seen']:raise StoreBlocked('review evidence changed/stale')
    observed['reviews']=r.get('reviews',[]);r=observed
   review=row['review']
   if type(review) is not dict or set(review)!={'status','operator','operator_identity','at','note','execution_authorized'} or review['operator_identity']!='LOCAL_UNAUTHENTICATED_INPUT':raise StoreBlocked('local review identity/schema')
   target=review['status'];label(review['operator'],64);number(review['at'])
   if review['execution_authorized'] is not False or target not in TRANSITIONS or type(review['note']) is not str or len(review['note'])>400:raise StoreBlocked('review schema')
   if not legacy and not review['note'].strip():raise StoreBlocked('review reason required')
   if not legacy and (row.get('expected_review')!=found[1] or target not in TRANSITIONS[found[1]]):raise StoreBlocked('duplicate/stale transition')
   r['review']=target;r['reviews']=(r.get('reviews',[])+[review])[-16:];validate_row(r)
   self.db.execute('UPDATE incidents SET payload=?,review=?,state=?,severity=?,seen=? WHERE id=?',(json.dumps(r,ensure_ascii=False,allow_nan=False),target,r['state'],r['severity'],r['last_seen'],r['id']))
  else:raise StoreBlocked('unsupported audit action')
  self.db.execute('INSERT INTO audit(uid,payload) VALUES(?,?)',(uid,raw))
 def append(self,row):return self._transaction(lambda:self._write(copy.deepcopy(row)))
 def _decode(self,record):
  if not record:return None
  row=json.loads(record[0]);row['review']=record[1];return validate_row(row)
 def get(self,incident_id):
  with self.lock:self._check();return self._decode(self.db.execute('SELECT payload,review FROM incidents WHERE id=?',(incident_id,)).fetchone())
 def catalog(self,scope='ALL',offset=0,limit=100):
  if scope not in ('ALL','CURRENT','HISTORY','UNRESOLVED') or type(offset) is not int or offset<0 or not 1<=limit<=100:raise StoreBlocked('query bound')
  where={'ALL':'1','CURRENT':"state='CURRENT'",'HISTORY':"state='HISTORY'",'UNRESOLVED':"review IN ('PENDING','HELD')"}[scope]
  order="CASE severity WHEN 'HIGH' THEN 0 WHEN 'CAUTION' THEN 1 ELSE 2 END,seen DESC" if scope in ('CURRENT','UNRESOLVED') else 'seen DESC,id DESC'
  with self.lock:
   self._check();return [self._decode(r) for r in self.db.execute('SELECT payload,review FROM incidents WHERE '+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',(limit,offset))]
 def unresolved_counts(self):
  with self.lock:
   self._check();return dict(self.db.execute("SELECT review,count(*) FROM incidents WHERE review IN ('PENDING','HELD') GROUP BY review"))
 def restore(self,now):
  def archive():
   for record in self.db.execute("SELECT payload,review FROM incidents WHERE state='CURRENT'").fetchall():
    r=self._decode(record);r.update(state='HISTORY',end_reason='APP_RESTART_UNVERIFIED',ended_at=now);self._write(dict(action='INCIDENT_ENDED',incident=r))
  self._transaction(archive)
  return self.catalog('HISTORY',limit=100)
 def import_jsonl(self,path):
  path=Path(path)
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
  try:
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_nlink!=1 or st.st_size>4*1024*1024:raise StoreBlocked('legacy file identity/bound')
   raw=b''
   while len(raw)<=4*1024*1024:
    chunk=os.read(fd,65536)
    if not chunk:break
    raw+=chunk
   after=os.fstat(fd)
   if (st.st_ino,st.st_mtime_ns,st.st_size)!=(after.st_ino,after.st_mtime_ns,after.st_size):raise StoreBlocked('legacy file changed during import')
  finally:os.close(fd)
  if len(raw)>4*1024*1024 or (raw and not raw.endswith(b'\n')):raise StoreBlocked('legacy journal truncated/oversize')
  digest=hashlib.sha256(raw).hexdigest();rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
  def migrate():
   if self.db.execute('SELECT 1 FROM imports WHERE sha=?',(digest,)).fetchone():return False
   new_rows=[r for r in rows if not self.db.execute('SELECT 1 FROM audit WHERE uid=?',(hashlib.sha256(json.dumps(r,ensure_ascii=False,allow_nan=False,sort_keys=True).encode()).hexdigest(),)).fetchone()]
   ids={r['incident']['id'] if 'incident' in r else r['incident_id'] for r in new_rows}
   if any(self.db.execute('SELECT 1 FROM incidents WHERE id=?',(identity,)).fetchone() for identity in ids):raise StoreBlocked('legacy ID collides with existing stored records')
   for row in new_rows:self._write(row,legacy=True)
   self.db.execute('INSERT INTO imports VALUES(?)',(digest,));return True
  return self._transaction(migrate)
 def empty(self):
  with self.lock:self._check();return self.db.execute('SELECT count(*) FROM incidents').fetchone()[0]==0
 def close(self):
  if self.db is not None:self.db.close();self.db=None
  if self.lockfd is not None:os.close(self.lockfd);self.lockfd=None
  self.closed=True
