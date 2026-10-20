"""Long real-video shadow validation; shared YOLO/ByteTrack, fixed policies.

File replay at source-video timestamps, not live Qt/PX4 throughput or safety GT.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import time

from crowd_regions import CandidateConfig, CrowdRegions
from prepare_fire_dataset import iou
from replay_tracker import ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--sample-hz',type=float,default=10)
    parser.add_argument('--videos',nargs='+',type=Path,default=[ROOT/'videos/7949326-hd_1920_1080_30fps.mp4',ROOT/'videos/7949436-hd_1920_1080_30fps.mp4'])
    args=parser.parse_args();out=args.output.resolve()
    if out.exists():parser.error('Use a fresh output directory')
    if args.seconds<=0 or args.sample_hz<1:parser.error('Positive duration and sample rate >=1 required')
    out.mkdir(parents=True)
    import cv2
    from ultralytics import YOLO
    watched=[ROOT/'legacy/drone_tracker.py',ROOT/'tools/ai/crowd_regions.py',ROOT/'models/yolo11s.pt',ROOT/'models/fire_smoke_best.pt',ROOT/'tools/gazebo/x500_camera.sdf']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched};summaries=[]
    for video in args.videos:
        video=video.resolve();folder=out/video.stem;folder.mkdir()
        cap=cv2.VideoCapture(str(video));fps=cap.get(cv2.CAP_PROP_FPS);count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or fps<=0 or args.sample_hz>fps:raise ValueError('Invalid video/sample rate')
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        model=YOLO(str(ROOT/'models/yolo11s.pt'));candidate=CrowdRegions();rows=[];seen={};old_hits=0;old_until=-1
        previous_old=False;previous_new='NORMAL';old_transitions=[];new_transitions=[]
        writer=cv2.VideoWriter(str(folder/'comparison.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),args.sample_hz,(1280,720))
        if not writer.isOpened():raise RuntimeError('Video writer unavailable')
        native=-1;index=0;started=time.perf_counter()
        try:
            while index/args.sample_hz<min(args.seconds,count/fps):
                target=round(index*fps/args.sample_hz)
                while native<target-1:
                    if not cap.grab():break
                    native+=1
                ok,frame=cap.read();native+=1
                if not ok:break
                timestamp=native/fps;inference_start=time.perf_counter()
                result=model.track(frame,device='cpu',persist=True,tracker='bytetrack.yaml',conf=.35,iou=.5,classes=[0,2,3,5,7],imgsz=832,verbose=False)[0]
                model_ms=(time.perf_counter()-inference_start)*1000
                people=[dict(id=int(b.id[0]),class_id=0,confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in result.boxes if int(b.cls[0])==0 and b.id is not None]
                unique={p['id']:p for p in people}
                for tid in unique:seen.setdefault(str(tid),[]).append(index)
                if (index+1)%10==0:
                    old_hits=max(0,old_hits+(1 if len(unique)>=8 else -1))
                    if old_hits>=4:old_until=timestamp+6;old_hits=0
                old_active=timestamp<old_until
                if old_active!=previous_old:old_transitions.append(dict(seconds=timestamp,active=old_active));previous_old=old_active
                begin=time.perf_counter();new=candidate.update(people,timestamp,width,height);module_ms=(time.perf_counter()-begin)*1000
                new_changed=new['level']!=previous_new
                if new_changed:new_transitions.append(dict(seconds=timestamp,old=previous_new,new=new['level']));previous_new=new['level']
                overlap_pairs=[dict(ids=[a['id'],b['id']],iou=iou(a['xyxy'],b['xyxy'])) for i,a in enumerate(people) for b in people[i+1:] if a['id']!=b['id'] and iou(a['xyxy'],b['xyxy'])>=.5]
                rows.append(dict(index=index,native_frame=native,seconds=timestamp,tracked_people=len(unique),duplicate_id_rows=len(people)-len(unique),
                    overlap_candidates=overlap_pairs,people=people,legacy_active=old_active,legacy_hits=old_hits,candidate=new,
                    model_ms=model_ms,candidate_module_ms=module_ms))
                annotated=frame.copy()
                for p in people:
                    x,y,X,Y=map(round,p['xyxy']);cv2.rectangle(annotated,(x,y),(X,Y),(0,220,0),2)
                    cv2.putText(annotated,f"HUMAN {p['id']}",(x,max(18,y-5)),0,.5,(0,220,0),1)
                for g in new['groups']:
                    x,y,X,Y=map(round,g['xyxy']);color=(0,0,255) if g['level']=='HIGH' else (0,200,255)
                    cv2.rectangle(annotated,(x,y),(X,Y),color,3)
                    label='CANDIDATE' if g['level']=='NORMAL' else g['level']
                    cv2.putText(annotated,f"GROUP {g['group_id']} {label} N={g['people']} {'HOLD' if g['stale'] else ''}",(x,max(24,y-10)),0,.6,color,2)
                cv2.putText(annotated,'SHADOW TEST | IMAGE-SPACE / UNCALIBRATED',(10,30),0,.7,(0,200,255),2)
                cv2.putText(annotated,f"t={timestamp:.1f}s PEOPLE={len(unique)} | OLD CROWD={old_active} | NEW={new['level']}",(10,65),0,.7,(0,200,255),2)
                writer.write(cv2.resize(annotated,(1280,720)))
                if index%int(args.sample_hz)==0 or new_changed:
                    cv2.imwrite(str(folder/f'frame_{index:04d}.jpg'),annotated)
                if index%50==0:print(video.name,index,'people',len(unique),'old',old_active,'new',new['level'],flush=True)
                index+=1
        finally:cap.release();writer.release()
        elapsed=time.perf_counter()-started;check=cv2.VideoCapture(str(folder/'comparison.mp4'));encoded=int(check.get(cv2.CAP_PROP_FRAME_COUNT));check.release()
        summary=dict(video=video.name,video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),frames=len(rows),sample_hz=args.sample_hz,
            last_source_second=rows[-1]['seconds'] if rows else None,people_min=min(r['tracked_people'] for r in rows),people_max=max(r['tracked_people'] for r in rows),
            people_mean=statistics.mean(r['tracked_people'] for r in rows),unique_ids=len(seen),longest_id_observations=max(map(len,seen.values()),default=0),
            duplicate_id_rows=sum(r['duplicate_id_rows'] for r in rows),overlap_candidate_frames=sum(bool(r['overlap_candidates']) for r in rows),
            legacy_active_frames=sum(r['legacy_active'] for r in rows),new_caution_frames=sum(r['candidate']['level']=='CAUTION' for r in rows),
            new_high_frames=sum(r['candidate']['level']=='HIGH' for r in rows),old_transitions=old_transitions,new_transitions=new_transitions,
            mean_model_ms=statistics.mean(r['model_ms'] for r in rows),mean_candidate_module_ms=statistics.mean(r['candidate_module_ms'] for r in rows),
            file_loop_fps=len(rows)/elapsed,encoded_frames=encoded,comparison_video_decodes=encoded==len(rows))
        (folder/'results.json').write_text(json.dumps(dict(summary=summary,rows=rows),indent=2));summaries.append(summary)
        (out/'summary.json').write_text(json.dumps(dict(summaries=summaries,config=asdict(CandidateConfig()),hashes=hashes,
            unchanged={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(ROOT))] for p in watched},
            scope='Actual file decode + unchanged YOLO/ByteTrack, both rules share tracks and source timestamps. No operational UI/events/other model inference. No density/safety ground truth. File loop FPS includes render/encode and is not paced camera FPS.'),indent=2))


if __name__=='__main__':main()
