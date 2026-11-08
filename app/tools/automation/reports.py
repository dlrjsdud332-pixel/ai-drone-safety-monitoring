"""Bounded local derived reports. IncidentStore remains authoritative audit."""
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

class Reports:
    def __init__(self,path):
        self.root=Path(path);self.root.mkdir(parents=True,exist_ok=True)
        if self.root.is_symlink():raise ValueError('report directory symlink')
        self.lock=threading.RLock();self.status='OK';self.error=None
        db=self.root/'reports.sqlite3'
        if db.is_symlink():raise ValueError('report database symlink')
        self.db=sqlite3.connect(db,check_same_thread=False);self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA max_page_count=16384')
        self.db.execute('CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,payload TEXT,digest TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS patrol(n INTEGER PRIMARY KEY,row TEXT)');self.db.commit()
    def atomic(self,path,data):
        fd,name=tempfile.mkstemp(prefix='.report-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            os.replace(name,path)
            d=os.open(self.root,os.O_RDONLY);os.fsync(d);os.close(d)
        finally:
            if os.path.exists(name):os.unlink(name) # only this invocation's private temp
    def patrol(self,row):
        with self.lock:
            if self.db.execute('SELECT count(*) FROM patrol').fetchone()[0]>=10000:raise ValueError('patrol history capacity')
            self.db.execute('INSERT INTO patrol(row) VALUES(?)',(json.dumps(row,allow_nan=False),));self.db.commit()
    def history(self):
        with self.lock:return [json.loads(r[0]) for r in self.db.execute('SELECT row FROM patrol ORDER BY n')]
    def get(self,identity):
        with self.lock:
            v=self.db.execute('SELECT payload,digest FROM reports WHERE id=?',(identity,)).fetchone()
            if v is None:return None
            try:
                if hashlib.sha256(v[0].encode()).hexdigest()!=v[1]:raise ValueError('report checksum')
                p=json.loads(v[0])
                if p['mode']!='OFFLINE_DEMO' or p['incident']['id']!=identity or p['execution_authorized'] is not False or p['evidence_frame'] not in (None,self.key(identity)+'.jpg'):raise ValueError('report identity')
                return p
            except Exception:
                self.status='BLOCKED';raise
    def key(self,identity):return hashlib.sha256(identity.encode()).hexdigest()
    def render(self,p):
        row=p['incident'];return '# OFFLINE_DEMO 사건 보고서\n\n사고 미확정 · 현장 조치 UNKNOWN · 비행 승인 없음\n\n'+ '\n'.join(f'- {k}: {v}' for k,v in row.items() if k not in ('evidence','reviews'))+'\n\n## YOLO / 탐지 근거 (점수 ≠ 사고 확률)\n\n```json\n'+json.dumps(row['evidence'],ensure_ascii=False,indent=2)+'\n```\n\n## 처리·상태 이력\n\n```json\n'+json.dumps(dict(changes=p.get('state_transitions',[]),detection_state=row['state'],end_reason=row.get('end_reason','UNKNOWN')),ensure_ascii=False,indent=2)+'\n```\n\n## EXPERIMENTAL 연기 영상 이동\n\n'+json.dumps(p['smoke_flow'],ensure_ascii=False)+'\n\n미션/기체 관측 (없으면 UNKNOWN): '+json.dumps(p.get('flight_context') or 'UNKNOWN',ensure_ascii=False)+'\n\n실제 검증: '+p['real_validation']+'\n\n증거 프레임: '+str(p['evidence_frame'])+'\n'
    def update(self,row,flow=None,frame=None,patrol=None,flight_context=None):
        with self.lock:
            try:
                key=self.key(row['id']);old=self.get(row['id']);image=old.get('evidence_frame') if old else None
                if old is None and self.db.execute('SELECT count(*) FROM reports').fetchone()[0]>=5000:raise ValueError('report capacity')
                if image is None and frame is not None:
                    import cv2
                    h,w=frame.shape[:2];scaled=cv2.resize(frame,(min(960,w),max(1,int(h*min(960,w)/w))));ok,data=cv2.imencode('.jpg',scaled,[cv2.IMWRITE_JPEG_QUALITY,80])
                    if not ok:raise OSError('frame encoding')
                    image=key+'.jpg';self.atomic(self.root/image,data.tobytes())
                transitions=[dict(at=row['first_seen'],detection_state='CURRENT',meaning='FIRST_VALID_OBSERVATION')]
                if row['state']=='HISTORY':transitions.append(dict(at=row.get('ended_at'),detection_state='HISTORY',reason=row.get('end_reason','UNKNOWN'),risk_resolved=False))
                transitions.extend(dict(at=r['at'],human_status=r['status'],operator=r['operator'],reason=r['note']) for r in row['reviews'])
                transitions.sort(key=lambda v:v['at'] or 0)
                p=dict(schema=1,mode='OFFLINE_DEMO',flight_context=old.get('flight_context') if old and old.get('flight_context') is not None else flight_context,incident=row,state_transitions=transitions,smoke_flow=flow or dict(direction='UNKNOWN',label='EXPERIMENTAL / IMAGE ONLY'),evidence_frame=image,real_validation='UNVERIFIED / recorded video or fixture; incident unconfirmed',simulation_at_creation=old.get('simulation_at_creation') if old else patrol,execution_authorized=False)
                data=json.dumps(p,ensure_ascii=False,allow_nan=False,sort_keys=True,indent=2);digest=hashlib.sha256(data.encode()).hexdigest()
                # Repair missing/partial exports even if authoritative content unchanged.
                md=self.render(p)
                if image:md+='\n![OFFLINE_DEMO evidence frame]('+image+')\n'
                exports={'.json':data.encode(),'.md':md.encode(),'.html':('<!doctype html><meta charset="utf-8"><title>OFFLINE_DEMO</title><pre>'+html.escape(md)+'</pre>'+('<img alt="OFFLINE_DEMO evidence" src="'+image+'">' if image else '')).encode()}
                for suffix,blob in exports.items():
                    path=self.root/(key+suffix)
                    if path.is_symlink():raise ValueError('report output symlink')
                    if not path.exists() or path.read_bytes()!=blob:self.atomic(path,blob)
                self.db.execute('INSERT INTO reports VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,digest=excluded.digest',(row['id'],data,digest));self.db.commit();self.status='OK';self.error=None
                return p
            except Exception as e:
                self.db.rollback();self.status='BLOCKED';self.error=type(e).__name__;raise
    def close(self):self.db.close()
