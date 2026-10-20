"""Offline crowd detector comparison. Never modifies production or weights.

Annotations are unreviewed by default. Only externally confirmed complete frames
may produce precision/recall; prediction count is never treated as accuracy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
METHODS = ('baseline832', 'high1280', 'tiles832')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--frames', type=int, default=30)
    p.add_argument('--paced-only', action='store_true', help='Only run cached-frame 10Hz timing test')
    p.add_argument('--paced-seconds', type=int, default=6)
    a = p.parse_args()
    if a.output.exists() or a.frames < 1 or a.paced_seconds < 1:
        p.error('New output directory and positive durations required')
    import cv2
    import numpy as np
    import torch
    from torchvision.ops import batched_nms
    from ultralytics import YOLO
    from ultralytics.engine.results import Boxes
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import YAML
    from ultralytics.utils.checks import check_yaml
    out = a.output.resolve(); out.mkdir(parents=True)
    watched = [ROOT / s for s in ('legacy/drone_tracker.py', 'tools/ai/crowd_regions.py', 'models/yolo11s.pt', 'models/fire_smoke_best.pt', 'tools/gazebo/x500_camera.sdf')]
    hashes = {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in watched}
    model = YOLO(str(ROOT/'models/yolo11s.pt'))
    cfg = SimpleNamespace(**YAML.load(check_yaml('bytetrack.yaml')))
    videos = [ROOT/'videos'/f'{s}-hd_1920_1080_30fps.mp4' for s in ('7949326','7949436','4825783')]
    def infer(frame, method):
        h,w = frame.shape[:2]
        starts = [(0,0,w,h)]
        if method == 'tiles832':
            # Six 50% width x 60% height tiles: 50% horizontal, 33% vertical overlap.
            tw,th = round(w*.5),round(h*.6)
            starts = [(x,y,x+tw,y+th) for y in (0,h-th) for x in (0,(w-tw)//2,w-tw)]
        parts=[]
        for x,y,X,Y in starts:
            r=model.predict(frame[y:Y,x:X],device='cpu',conf=.35,iou=.5,
                classes=[0,2,3,5,7],imgsz=1280 if method=='high1280' else 832,verbose=False)[0]
            b=r.boxes.data.cpu().clone()
            if len(b):
                b[:,[0,2]]+=x;b[:,[1,3]]+=y;parts.append(b)
        b=torch.cat(parts) if parts else torch.empty((0,6))
        before=len(b)
        if method=='tiles832' and len(b):
            b=b[batched_nms(b[:,:4],b[:,4],b[:,5],.5)][:300]
        return b.numpy(),before
    def analyze(frame,method,tracker):
        t=time.perf_counter(); boxes,pre=infer(frame,method); model_ms=(time.perf_counter()-t)*1000
        t=time.perf_counter();tracks=tracker.update(Boxes(boxes,frame.shape[:2]).cpu().numpy(),frame)
        track_ms=(time.perf_counter()-t)*1000
        people=[dict(xyxy=b[:4].tolist(),confidence=float(b[4])) for b in boxes if int(b[5])==0]
        human=[dict(xyxy=b[:4].tolist(),id=int(b[4]),confidence=float(b[5])) for b in tracks if int(b[6])==0]
        return dict(detections=people,tracks=human,pre_merge_all_classes=pre,model_ms=model_ms,track_ms=track_ms,
                    duplicate_id_rows=len(human)-len({b['id'] for b in human}))
    # Warm each shape, excluded from timing.
    blank=np.zeros((1080,1920,3),np.uint8)
    for method in METHODS:infer(blank,method)
    summaries=[];review=[]
    for video in ([] if a.paced_only else videos):
        cap=cv2.VideoCapture(str(video));fps=cap.get(cv2.CAP_PROP_FPS)
        if not cap.isOpened() or fps<=0:raise RuntimeError(str(video))
        frames=[]
        for i in range(a.frames):
            native=round((5+i/10)*fps);cap.set(cv2.CAP_PROP_POS_FRAMES,native);ok,f=cap.read()
            if not ok:break
            frames.append((native,f))
        cap.release()
        folder=out/video.stem;folder.mkdir()
        for i in (0,len(frames)//2,len(frames)-1):
            native,f=frames[i];key=f'{video.stem}_{native}'
            cv2.imwrite(str(folder/f'{key}.png'),f)
            review.append(dict(key=key,image=f'{video.stem}/{key}.png',video=video.name,native_frame=native,
                status='unreviewed',complete=False,people=[],ignore_regions=[],note='Add ALL visible person boxes or explicitly mark ignored crowd regions. Predictions are not ground truth.'))
        for method in METHODS:
            tracker=BYTETracker(cfg);rows=[];beg=time.perf_counter()
            for i,(native,f) in enumerate(frames):
                r=analyze(f,method,tracker);r.update(index=i,native_frame=native);rows.append(r)
                if i in (0,len(frames)//2,len(frames)-1):
                    im=f.copy()
                    for b in r['detections']:
                        x,y,X,Y=map(round,b['xyxy']);cv2.rectangle(im,(x,y),(X,Y),(0,180,255),1)
                    for b in r['tracks']:
                        x,y,X,Y=map(round,b['xyxy']);cv2.putText(im,str(b['id']),(x,y),0,.4,(0,255,0),1)
                    cv2.putText(im,f"{method} raw={len(r['detections'])} tracked={len(r['tracks'])}",(20,35),0,1,(0,0,255),2)
                    cv2.imwrite(str(folder/f'{native}_{method}.jpg'),im)
            elapsed=time.perf_counter()-beg
            times=[r['model_ms']+r['track_ms'] for r in rows]
            ids={b['id'] for r in rows for b in r['tracks']}
            summary=dict(video=video.name,method=method,frames=len(rows),raw_mean=float(np.mean([len(r['detections']) for r in rows])),
                tracked_mean=float(np.mean([len(r['tracks']) for r in rows])),tracked_max=max(len(r['tracks']) for r in rows),
                unique_ids=len(ids),duplicate_id_rows=sum(r['duplicate_id_rows'] for r in rows),
                model_mean_ms=float(np.mean([r['model_ms'] for r in rows])),total_mean_ms=float(np.mean(times)),p95_ms=float(np.percentile(times,95)),
                offline_fps=len(rows)/elapsed,accuracy=None,reason='No human-confirmed exhaustive person labels')
            (folder/f'{method}.json').write_text(json.dumps(dict(summary=summary,rows=rows),indent=2))
            summaries.append(summary);print(json.dumps(summary),flush=True)
    (out/'review.json').write_text(json.dumps(dict(schema=1,status='unreviewed',frames=review),indent=2))
    # Actual paced latest-frame replay: file source clock 10Hz; skipped source indices counted.
    video=videos[1];cap=cv2.VideoCapture(str(video));fps=cap.get(cv2.CAP_PROP_FPS);paced=[]
    cached=[]
    for slot in range(a.paced_seconds*10):
        cap.set(cv2.CAP_PROP_POS_FRAMES,round((5+slot/10)*fps));ok,f=cap.read()
        if not ok:raise RuntimeError('Paced preload decode failed')
        cached.append(f)
    cap.release()
    for method in METHODS:
        tracker=BYTETracker(cfg);rows=[];last=-1;start=time.perf_counter();slots=a.paced_seconds*10
        while True:
            elapsed=time.perf_counter()-start;idx=int(elapsed*10)
            if idx>=slots:break
            if idx==last:time.sleep(min(.005,(idx+1)/10-elapsed));continue
            f=cached[idx]
            r=analyze(f,method,tracker);r['slot']=idx;rows.append(r);last=idx
        elapsed=time.perf_counter()-start
        paced.append(dict(method=method,offered=slots,processed=len(rows),skipped=slots-len(rows),wall_seconds=elapsed,
            achieved_fps=len(rows)/elapsed,source='Predecoded real video frames; actual 10Hz clock and latest-slot skipping. Decode excluded.',rows=rows))
        print('PACED',method,len(rows),'/',slots,flush=True)
    cap.release()
    report=dict(summaries=summaries,paced=paced,hashes=hashes,unchanged={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest()==hashes[str(f.relative_to(ROOT))] for f in watched},
        settings=dict(conf=.35,iou=.5,device='cpu',classes=[0,2,3,5,7],tracker='bytetrack.yaml',tile='6 overlapping crops; original coordinates; global class-aware NMS .5; max_det300'),
        scope='Offline comparison predecodes real source frames. Paced replay predecodes before clock; actual 10Hz latest-frame clock with inference and tracking, decode/UI/render/other models excluded. Not live-camera FPS. No operating changes. Accuracy pending human labels.')
    (out/'summary.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
