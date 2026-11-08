"""Optional observation-only evidence journal; never emits operational alarms or commands.

Only EventManager's existing confirmed events can put this audit agent in ALERT.
Repeated raw candidates can become VERIFYING, never FIRE confirmation. Spatial
association is image-space diagnostic evidence, not a calibrated fire-spread model.
"""
from collections import deque
import json
import math
from pathlib import Path
import queue
import threading
import time

PHASE={'PATROL':'OBSERVE','SUSPECTED':'DETECT','VERIFYING':'VERIFY','ALERT':'ALERT','RESUME':'RESUME','WAITING':'HOLD'}

PRIORITY={'FIRE':5,'SMOKE':4,'CROWD':3,'NO_HARDHAT':2,'NO_VEST':2,'INTRUSION':3,'FALL':4}

class EvidenceAgent:
    def __init__(self):
        self.channels={};self.events=deque(maxlen=2048)
    def _channel(self, channel):
        return self.channels.setdefault(str(channel),dict(state='PATROL',sequence=-1,sites=[],active=[],last_seen=None))
    def observe(self, channel, sequence, captured_at, now, candidates):
        c=self._channel(channel)
        if not all(math.isfinite(t) for t in (captured_at,now)) or captured_at<=0 or now-captured_at>1 or captured_at>now+.05:
            c['state']='WAITING'
            return dict(state='WAITING',phase='HOLD',reason='STALE_OR_INVALID_FRAME',commands=[],confirmed_types=[])
        if sequence<=c['sequence']:
            return dict(state=c['state'],phase=PHASE[c['state']],reason='DUPLICATE_OR_OUT_OF_ORDER_FRAME',commands=[],confirmed_types=sorted({e['kind'] for e in c['active']}))
        c['sequence']=sequence
        c['active']=[e for e in c['active'] if 0<=now-e['at']<=6]
        sites=[s for s in c['sites'] if 0<=now-s['at']<=2]
        for item in candidates:
            kind=item.get('kind');conf=item.get('confidence',0);box=item.get('xyxy',[])
            if kind not in ('FIRE','SMOKE') or not isinstance(conf,(float,int)) or not math.isfinite(conf):continue
            if conf < (.35 if kind=='FIRE' else .50) or not item.get('display_eligible',False):continue
            if len(box)!=4 or not all(math.isfinite(float(v)) for v in box) or box[2]<=box[0] or box[3]<=box[1]:continue
            cx=(box[0]+box[2])/2;cy=(box[1]+box[3])/2
            same=[s for s in sites if s['kind']==kind and (s['center'][0]-cx)**2+(s['center'][1]-cy)**2<=40**2]
            previous=min(same,key=lambda s:(s['center'][0]-cx)**2+(s['center'][1]-cy)**2) if same else None
            if previous:
                # One hit per analysis frame, even if NMS leaves duplicate boxes.
                if previous['sequence']==sequence:continue
                previous.update(center=[cx,cy],at=now,sequence=sequence,hits=previous['hits']+1 if now-previous['at']<=2 else 1,confidence=conf)
            else:
                sites.append(dict(kind=kind,center=[cx,cy],at=now,sequence=sequence,hits=1,confidence=conf))
        c['sites']=sites[-64:]
        current=[s for s in sites if now-s['at']<=2]
        pairs=[(f,s) for f in current for s in current if f['kind']=='FIRE' and s['kind']=='SMOKE' and abs(f['at']-s['at'])<=2 and math.dist(f['center'],s['center'])<150]
        old=c['state']
        if c['active']:state,reason='ALERT','EXISTING_CONFIRMED_EVENT'
        elif current:state,reason=('VERIFYING' if max(s['hits'] for s in current)>=4 else 'SUSPECTED'),'MODEL_CANDIDATE_ONLY'
        elif old in ('ALERT','VERIFYING','SUSPECTED'):state,reason='RESUME','EVIDENCE_EXPIRED'
        else:state,reason='PATROL','NO_ACTIVE_EVIDENCE'
        c['state']=state
        return dict(state=state,phase=PHASE[state],reason=reason,commands=[],confirmed_types=sorted({e['kind'] for e in c['active']}),
                    candidate_types=sorted({s['kind'] for s in current}),image_space_fire_smoke_pairs=len(pairs),
                    sites=current,decision_changed=old!=state)
    def runtime(self, **values):
        telemetry=values.get('telemetry',{})
        unsafe=(telemetry.get('telemetry_source')=='PX4' and not (
            telemetry.get('telemetry_connected') and telemetry.get('gps_valid') and telemetry.get('home_valid')))
        return dict(values, agent_phase='HOLD' if unsafe else 'OBSERVE',
                    agent_reason='PX4_LINK_OR_POSITION_UNSAFE' if unsafe else 'OBSERVATION_ONLY',
                    px4_flight_mode=telemetry.get('flight_mode'), commands=[])
    def confirmed(self, meta, now):
        # This API accepts existing EventManager event metadata, never raw model boxes.
        key=(meta.get('camera'),meta.get('event'),meta.get('event_id'))
        if not all(v is not None for v in key):return None
        if key in self.events:return None
        self.events.append(key);c=self._channel(meta['camera'])
        c['active'].append(dict(kind=meta['event'],at=now,event_id=meta['event_id']))
        c['active']=c['active'][-128:];c['state']='ALERT'
        return dict(state='ALERT',phase='RECORD' if meta.get('snapshot_available') else 'ALERT',
                    phase_sequence=['ALERT','RECORD'] if meta.get('snapshot_available') else ['ALERT'],
                    reason='EXISTING_CONFIRMED_EVENT',event=meta,commands=[],
                    priority=PRIORITY.get(meta['event'],1))

class EvidenceJournal:
    """Bounded queue, independent writer, archived 8MiB segments; no UI file I/O."""
    def __init__(self, folder, limit=8*1024*1024):
        self.folder=Path(folder);self.limit=limit;self.queue=queue.Queue(maxsize=128)
        self.dropped=0;self.error=None;self.agent=EvidenceAgent();self.stop_event=threading.Event()
        import atexit
        atexit.register(self.close)
        self.thread=threading.Thread(target=self._run,daemon=True,name='SafetyEvidenceWriter');self.thread.start()
    def _put(self,item):
        try:self.queue.put_nowait(item)
        except queue.Full:self.dropped+=1
    def observe(self,**values):self._put(('observe',values))
    def runtime(self,values):self._put(("runtime",values))
    def confirmed(self,meta):self._put(('confirmed',dict(meta=meta,now=time.monotonic())))
    def _run(self):
        try:
            self.folder.mkdir(parents=True,exist_ok=True)
            # Exclusive per-process journal; concurrent GUI sessions cannot share a file.
            import os
            path=self.folder/f'evidence_{os.getpid()}_{time.time_ns()}.jsonl'
            with path.open('x') as stream:
                size=0
                while not self.stop_event.is_set() or not self.queue.empty():
                    try:kind,values=self.queue.get(timeout=.1)
                    except queue.Empty:continue
                    result=getattr(self.agent,kind)(**values)
                    if result is None:continue
                    # Write state changes and all confirmed events; periodic traces bounded by queue.
                    record=dict(kind=kind,at_unix=time.time(),input=values,result=result,dropped_records=self.dropped)
                    text=json.dumps(record,ensure_ascii=False)+'\n';length=len(text.encode())
                    if length>self.limit:self.dropped+=1;continue
                    if size+length>self.limit:
                        stream.close()
                        path=self.folder/f'evidence_{os.getpid()}_{time.time_ns()}.jsonl'
                        stream=path.open('x');size=0
                    stream.write(text);stream.flush();size+=length
                stream.close()
        except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
    def close(self):
        self.stop_event.set();self.thread.join(timeout=3)


def runtime_snapshot(channels, telemetry, paints, previous_paints, elapsed, now):
    """Counter snapshots only: receiver completion, analysis completion, Qt paints.

    Qt paint events are not physical display presentation timestamps. No extra
    image inference/copies or socket readers are introduced.
    """
    rows=[]
    for c in channels:
        with c.lock:
            if not c.source:continue
            received=getattr(c,"received_at",0)
            analysis_received=getattr(c,"analysis_received_at",0)
            rows.append(dict(channel=c.number,source=c.source,
                camera_type='FIXED_OBSERVER' if '/observer/image' in c.source else 'INPUT_CAMERA',
                people=getattr(c,'people',None),vehicles=getattr(c,'vehicles',None),
                receive_sequence=c.sequence,analysis_sequence=getattr(c,"analysis_sequence",None),
                analysis_completed_count=getattr(c,"analysis_completed_count",0),
                selected_sequence_skips=getattr(c,"selected_sequence_skips",0),
                receive_fps=getattr(c,"receive_fps",None),analysis_fps=getattr(c,"analysis_fps",None),
                analysis_ms=getattr(c,"analysis_ms",None),
                source_age_ms=(now-received)*1000 if received>0 else None,
                boxes_age_ms=(now-analysis_received)*1000 if analysis_received>0 else None,
                target_switches=(getattr(c,"auto_result",None) or {}).get("target_switches"),
                preview_status=getattr(c.auto_roi_panel,"status",None),
                preview_status_changes=dict(c.auto_roi_panel.status_changes)))
    import resource
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return dict(at_monotonic=now,process_cpu_seconds=usage.ru_utime+usage.ru_stime,
                channels=rows,telemetry=telemetry,
                qt_paint_events_fps=(paints-previous_paints)/elapsed if elapsed>0 else None,
                physical_display_fps_verified=False,flight_commands_sent=False)
