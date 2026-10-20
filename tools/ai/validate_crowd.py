"""Production analyzer/ByteTrack crowd replays, isolated output; no Qt/PX4."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import time
from types import SimpleNamespace

from replay_tracker import ROOT, load_tracker
from prepare_fire_dataset import iou


CASES=[
 ('mixed_foreground','7949326-hd_1920_1080_30fps.mp4',2,False),
 ('fewer_detected','7949326-hd_1920_1080_30fps.mp4',19,False),
 ('dense_small_people','7949436-hd_1920_1080_30fps.mp4',2,False),
 ('zone_three_threshold','7949326-hd_1920_1080_30fps.mp4',19,True),
 ('zone_visible_people','7949326-hd_1920_1080_30fps.mp4',2,True),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--frames',type=int,default=90);parser.add_argument('--case',choices=[x[0] for x in CASES]);args=parser.parse_args();out=args.output.resolve()
    if out.exists():parser.error('Use a fresh output directory')
    out.mkdir(parents=True)
    import cv2
    from ultralytics import YOLO
    watched=[ROOT/'legacy/drone_tracker.py',ROOT/'models/yolo11s.pt',ROOT/'models/fire_smoke_best.pt',ROOT/'tools/gazebo/x500_camera.sdf']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    pose=YOLO(str(ROOT/'models/yolo11n-pose.pt'));ppe=YOLO(str(ROOT/'models/ppe_best.pt'));fire=YOLO(str(ROOT/'models/fire_smoke_best.pt'))
    summaries=[]
    for name,filename,start,zone in CASES:
        if args.case and args.case!=name:continue
        folder=out/name;folder.mkdir();app=load_tracker();app.ROOT=folder
        manager=app.EventManager(app.TelemetryState());c=app.Channel(1,'',True,manager)
        c.source=str(ROOT/'videos'/filename);c.status='Playing';analyzer=app.Analyzer([c],'cpu')
        state=app.AgentState(manager.telemetry);risk=app.RiskAlertEngine()
        model=YOLO(str(ROOT/'models/yolo11s.pt'));last=[None]
        def track(*a,**kw):last[0]=model.track(*a,**kw);return last[0]
        proxy=SimpleNamespace(track=track);cap=cv2.VideoCapture(c.source);fps=cap.get(cv2.CAP_PROP_FPS)
        cap.set(cv2.CAP_PROP_POS_MSEC,start*1000);rows=[];events=[];lifetimes={};previous={};index=0
        original_emit=manager.emit
        def emit(channel,kind,*a,**kw):
            before=set(folder.glob('data/events/**/event.json'));accepted=original_emit(channel,kind,*a,**kw)
            if accepted:
                for path in set(folder.glob('data/events/**/event.json'))-before:
                    meta=json.loads(path.read_text());expected=cv2.imencode('.jpg',frame)[1].tobytes()
                    events.append(dict(frame_index=index,kind=kind,metadata=str(path),details=meta['details'],
                        snapshot_matches_inference_frame=Path(meta['snapshot']).read_bytes()==expected))
            return accepted
        manager.emit=emit
        try:
            for index in range(args.frames):
                ok,frame=cap.read()
                if not ok:break
                if zone:c.danger=(0,0,frame.shape[1]-1,frame.shape[0]-1)
                c.publish(frame);t=time.perf_counter();analyzer.process(c,proxy,pose,ppe,fire,frame)
                elapsed=(time.perf_counter()-t)*1000
                agent=state.update([c]);alert=risk.evaluate(agent)
                people={tid:box for tid,(cls,box) in c.objects.items() if cls==0}
                for tid in people:lifetimes.setdefault(str(tid),[]).append(index)
                raw=last[0][0];tracked=[dict(id=int(b.id[0]) if b.id is not None else None,
                    confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in raw.boxes if int(b.cls[0])==0]
                overlaps=[dict(ids=[a,b],iou=iou(A,B)) for a,A in people.items() for b,B in people.items() if a<b and iou(A,B)>=.5]
                switches=[]
                for tid,box in people.items():
                    if tid in previous:continue
                    matches=[(iou(box,old),old_id) for old_id,old in previous.items() if old_id not in people]
                    score,old_id=max(matches,default=(0,None))
                    if score>=.5:switches.append(dict(old_id=old_id,new_id=tid,iou=score))
                rows.append(dict(frame_index=index,source_seconds=start+index/fps,wall_ms=elapsed,
                    tracked_people=len(people),track_result_person_boxes=len(tracked),tracks=tracked,
                    overlapping_id_candidates=overlaps,possible_id_switches=switches,
                    crowd_hits=c.crowd_hits,crowd_active=time.time()<c.crowd_until,
                    crowd_remaining_seconds=max(0,c.crowd_until-time.time()),inside_people=len(c.previous_danger),
                    inspect=c.frame_count%10==0,risk=alert))
                if index%10==9 or any(e['frame_index']==index and e['kind']=='CROWD' for e in events):
                    cv2.imwrite(str(folder/f'overlay_{index:04d}.jpg'),c.image)
                previous=people
        finally:c.stop();cap.release()
        time.sleep(.3)
        crowd_events=[e for e in events if e['kind']=='CROWD'];counts=[r['tracked_people'] for r in rows]
        summary=dict(case=name,video=filename,start_seconds=start,zone_full_frame=zone,frames=len(rows),
            tracked_people_min=min(counts),tracked_people_max=max(counts),tracked_people_mean=statistics.mean(counts),
            unique_person_ids=len(lifetimes),longest_id_observations=max(map(len,lifetimes.values()),default=0),
            overlapping_id_candidates=sum(len(r['overlapping_id_candidates']) for r in rows),
            possible_id_switches=sum(len(r['possible_id_switches']) for r in rows),
            crowd_active_frames=sum(r['crowd_active'] for r in rows),crowd_events=crowd_events,
            mean_process_ms=statistics.mean(r['wall_ms'] for r in rows))
        (folder/'replay.json').write_text(json.dumps(dict(summary=summary,events=events,rows=rows,
            lifetimes={k:dict(observations=len(v),first=v[0],last=v[-1],gaps=sum(b-a>1 for a,b in zip(v,v[1:]))) for k,v in lifetimes.items()},
            scope='Real models, production Analyzer, offline contiguous replay. No Qt display, real-world density labels, telemetry, or physical distance ground truth.'),indent=2))
        summaries.append(summary);print(name,json.dumps(summary),flush=True)
    (out/'summary.json').write_text(json.dumps(dict(summaries=summaries,hashes=hashes,
        unchanged={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(ROOT))] for p in watched}),indent=2))


if __name__=='__main__':main()
