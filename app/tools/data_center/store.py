"""Versioned append-only observations. Existing incident audit remains authoritative."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Protocol

KINDS = {'telemetry', 'detection_event', 'agent_decision', 'incident_report', 'mission_state', 'review'}
SOURCES = {'SIMULATOR', 'OFFLINE_DEMO', 'LIVE'}

class Repository(Protocol):
    def ingest(self, message): ...
    def search(self, **filters): ...

class DataCenter:
    def __init__(self, root, maximum_bytes=64*1024*1024, retention_days=30, clock=time.time):
        if maximum_bytes < 65536 or retention_days <= 0: raise ValueError('INVALID_CONFIG')
        self.root=Path(root); self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or (self.root/'hub.sqlite3').is_symlink(): raise ValueError('INVALID_PATH')
        self.clock=clock; self.retention_days=retention_days; self.lock=threading.RLock()
        self.db=sqlite3.connect(self.root/'hub.sqlite3', check_same_thread=False)
        if self.db.execute('PRAGMA user_version').fetchone()[0] not in (0,1):
            self.db.close();raise ValueError('UNSUPPORTED_DATABASE_VERSION')
        self.db.execute('PRAGMA synchronous=FULL')
        page=self.db.execute('PRAGMA page_size').fetchone()[0]
        self.db.execute('PRAGMA max_page_count='+str(maximum_bytes//page))
        self.db.executescript('''CREATE TABLE IF NOT EXISTS records(
          event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, drone_id TEXT, mission_id TEXT,
          incident_id TEXT, source TEXT NOT NULL, observed_at REAL NOT NULL,
          received_at REAL NOT NULL, digest TEXT NOT NULL, message TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS lookup ON records(drone_id,mission_id,incident_id,observed_at);
          PRAGMA user_version=1;'''); self.db.commit()
    def ingest(self, m):
        required={'schema_version','event_id','kind','drone_id','mission_id','incident_id','source','observed_at','clock','payload'}
        if not isinstance(m,dict) or set(m)!=required: raise ValueError('INVALID_SCHEMA')
        if m['schema_version']!='1.0' or m['kind'] not in KINDS or m['source'] not in SOURCES or m['clock']!='UTC_UNIX_SECONDS': raise ValueError('UNSUPPORTED_SCHEMA')
        for key in ('event_id','drone_id','mission_id','incident_id'):
            if m[key] is None and key!='event_id': continue
            if not isinstance(m[key],str) or not 1<=len(m[key])<=200: raise ValueError('INVALID_ID')
        at=m['observed_at']; now=self.clock()
        if type(at) not in (int,float) or not math.isfinite(at) or at<0 or at>now+2: raise ValueError('INVALID_CLOCK')
        if not isinstance(m['payload'],dict): raise ValueError('INVALID_PAYLOAD')
        raw=json.dumps(m,sort_keys=True,ensure_ascii=False,allow_nan=False)
        if len(raw.encode())>262144: raise ValueError('TOO_LARGE')
        digest=hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            old=self.db.execute('SELECT digest FROM records WHERE event_id=?',(m['event_id'],)).fetchone()
            if old:
                if old[0]!=digest: raise ValueError('ID_CONFLICT')
                return {'event_id':m['event_id'],'code':'DUPLICATE','execution_authorized':False}
            with self.db:
                self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)',(m['event_id'],m['kind'],m['drone_id'],m['mission_id'],m['incident_id'],m['source'],at,now,digest,raw))
        return {'event_id':m['event_id'],'code':'STORED','execution_authorized':False}
    def search(self, limit=100, offset=0, **filters):
        if not 1<=limit<=1000 or offset<0: raise ValueError('INVALID_PAGE')
        if set(filters)-{'drone_id','mission_id','incident_id','kind','source'}: raise ValueError('INVALID_FILTER')
        where=' AND '.join(k+'=?' for k in filters) or '1'
        with self.lock:
            rows=self.db.execute('SELECT message,received_at FROM records WHERE '+where+' ORDER BY observed_at DESC,event_id LIMIT ? OFFSET ?',(*filters.values(),limit,offset)).fetchall()
        return [dict(json.loads(r),received_at=received) for r,received in rows]
    def latest_telemetry(self, drone_id, source, ttl=2):
        rows=self.search(drone_id=drone_id,source=source,kind='telemetry',limit=1)
        if not rows: return {'freshness':'UNKNOWN','values':None}
        m=rows[0]; age=self.clock()-m['observed_at']
        fresh=0<=age<=ttl
        return {'freshness':'CURRENT' if fresh else 'STALE','values':m['payload'] if fresh else None,'last_observation':m}
    def export(self, format='json', **filters):
        # Caller explicitly saves locally; no automatic upload, no network listener.
        rows=[]; offset=0
        while True:
            page=self.search(limit=1000,offset=offset,**filters); rows.extend(page)
            if len(page)<1000: break
            offset+=len(page)
        if format=='json': return json.dumps(rows,ensure_ascii=False,allow_nan=False,indent=2)
        if format!='csv': raise ValueError('INVALID_FORMAT')
        out=io.StringIO(); writer=csv.writer(out); writer.writerow(['event_id','kind','drone_id','mission_id','incident_id','source','observed_at','received_at','payload'])
        for row in rows:
            values=[row[k] for k in ('event_id','kind','drone_id','mission_id','incident_id','source','observed_at','received_at')]+[json.dumps(row['payload'],ensure_ascii=False)]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
        return out.getvalue()
    def prune_telemetry(self):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM records WHERE kind='telemetry' AND observed_at<?",(self.clock()-self.retention_days*86400,)).rowcount
    def close(self): self.db.close()

def envelope(kind, payload, *, observed_at, drone_id=None, mission_id=None, incident_id=None, source='OFFLINE_DEMO'):
    m=dict(schema_version='1.0',kind=kind,payload=payload,observed_at=observed_at,drone_id=drone_id,mission_id=mission_id,incident_id=incident_id,source=source,clock='UTC_UNIX_SECONDS')
    m['event_id']=hashlib.sha256(json.dumps(m,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return m
