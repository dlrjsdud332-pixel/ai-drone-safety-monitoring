"""Replay real frames through the unchanged Channel/Analyzer/EventManager.

Only ROOT for event output is redirected into this diagnostic run. No Qt window,
PX4 sockets, flight adapters, fake detector outputs or weight changes.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]


def load_tracker():
    spec=importlib.util.spec_from_file_location('tracker_under_test',ROOT/'legacy/drone_tracker.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',type=Path,required=True)
    parser.add_argument('--start',type=float,default=2)
    parser.add_argument('--frames',type=int,default=60)
    parser.add_argument('--device',default='cpu')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    import cv2
    from ultralytics import YOLO
    app=load_tracker()
    output=args.output.resolve()
    if output.exists():raise SystemExit('Use a new output directory to avoid mixing prior events')
    output.mkdir(parents=True)
    tracker_hash=hashlib.sha256((ROOT/'legacy/drone_tracker.py').read_bytes()).hexdigest()
    # Same production classes; isolate all data/events, queues and CSV writes.
    app.ROOT=output
    telemetry=app.TelemetryState()
    manager=app.EventManager(telemetry)
    channel=app.Channel(1,'',True,manager)
    channel.source=str(args.video.resolve())
    channel.status='Playing'
    analyzer=app.Analyzer([channel],args.device)
    state=app.AgentState(telemetry)
    risk=app.RiskAlertEngine()
    person=YOLO(str(ROOT/'models/yolo11s.pt'))
    pose=YOLO(str(ROOT/'models/yolo11n-pose.pt'))
    ppe=YOLO(str(ROOT/'models/ppe_best.pt'))
    fire=YOLO(str(ROOT/'models/fire_smoke_best.pt'))
    cap=cv2.VideoCapture(str(args.video.resolve()))
    if not cap.isOpened():raise RuntimeError('Cannot open input')
    fps=cap.get(cv2.CAP_PROP_FPS)
    cap.set(cv2.CAP_PROP_POS_MSEC,args.start*1000)
    events=[];rows=[];seen={};index=0
    original_emit=manager.emit
    def emit(*a,**kwargs):
        pending=channel.pending.copy()
        prior=set(output.glob('data/events/**/event.json'))
        result=original_emit(*a,**kwargs)
        if result:
            for path in set(output.glob('data/events/**/event.json'))-prior:
                meta=json.loads(path.read_text())
                saved=cv2.imread(meta['snapshot'])
                # JPEG encoding is deterministic for the same frame/default settings.
                ok,expected=cv2.imencode('.jpg',pending)
                exact=ok and Path(meta['snapshot']).read_bytes()==expected.tobytes()
                events.append({'frame_index':index,'event':meta['event'],'metadata':str(path),
                    'snapshot_exactly_matches_pending_frame':bool(exact),'confidence':meta['confidence']})
        return result
    manager.emit=emit
    started=time.perf_counter()
    try:
        for index in range(args.frames):
            ok,frame=cap.read()
            if not ok:break
            channel.publish(frame)
            begin=time.perf_counter()
            analyzer.process(channel,person,pose,ppe,fire,frame)
            elapsed=(time.perf_counter()-begin)*1000
            state.update([channel]);snapshot=state.snapshot();alert=risk.evaluate(snapshot)
            active=snapshot['active_events']
            raw,display,objects=app.live_snapshot(channel)
            tracks=[]
            for track_id,(class_id,xyxy) in channel.objects.items():
                seen.setdefault(track_id,[]).append(index)
                tracks.append({'id':track_id,'class_id':class_id,'xyxy':list(xyxy)})
            rows.append({'frame_index':index,'source_seconds':args.start+index/fps,'wall_process_ms':elapsed,
                'tracks':tracks,'fire_hits':channel.fire_hits,'smoke_hits':channel.smoke_hits,
                'fire_active':time.time()<channel.fire_until,'smoke_active':time.time()<channel.smoke_until,
                'active_events':active,'risk':alert,'overlay_objects':len(objects),
                'inspect':channel.frame_count%10==0})
            if index%10==9 or any(e['frame_index']==index for e in events):
                cv2.imwrite(str(output/f'overlay_{index:04d}.jpg'),channel.image)
            if index%10==9:print('REPLAY',index+1,'frames','tracks',len(tracks),'events',active,flush=True)
    finally:
        channel.stop();cap.release()
    # Let original clip writer threads flush after channel stop; no full 10s post-roll claim.
    time.sleep(.5)
    lifetimes={str(tid):{'observations':len(indices),'first':indices[0],'last':indices[-1],
                         'gaps':sum(b-a>1 for a,b in zip(indices,indices[1:]))} for tid,indices in seen.items()}
    report={'video':str(args.video.resolve()),'device':args.device,'frames':len(rows),
        'wall_seconds':time.perf_counter()-started,'events':events,'tracks':lifetimes,'rows':rows,
        'tracker_sha256':tracker_hash,'qt_screen_verified':False,
        'scope':'Real production Analyzer.process/EventManager/live_snapshot on contiguous file frames; offline sequential replay, not latest-buffer live throughput or a Qt display test.',
        'event_clip_note':'Channel stopped at replay end; full post-roll duration not verified.'}
    (output/'replay.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'frames':len(rows),'events':events,'track_ids':len(seen)},indent=2),flush=True)


if __name__=='__main__':
    main()
