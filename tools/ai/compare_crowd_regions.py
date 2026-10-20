"""Shadow comparison on saved real YOLO/ByteTrack outputs; no operational writes."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import time

from crowd_regions import CandidateConfig, CrowdRegions
from replay_tracker import ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve()
    if out.exists():parser.error('Use a fresh output directory')
    out.mkdir(parents=True)
    import cv2
    files=sorted((ROOT/'diagnostics/crowd_validation/replay').glob('*/replay.json'))+sorted((ROOT/'diagnostics/crowd_validation/zone_positive').glob('*/replay.json'))
    watched=[ROOT/'legacy/drone_tracker.py',ROOT/'models/yolo11s.pt',ROOT/'models/fire_smoke_best.pt',ROOT/'tools/gazebo/x500_camera.sdf']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    summaries=[]
    for path in files:
        data=json.loads(path.read_text());meta=data['summary'];folder=out/meta['case'];folder.mkdir()
        detector=CrowdRegions();cap=cv2.VideoCapture(str(ROOT/'videos'/meta['video']));width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        zone=(0,0,width-1,height-1) if meta['zone_full_frame'] else None
        old_hits=0;old_until=0;rows=[]
        for row in data['rows']:
            now=row['source_seconds'];t=time.perf_counter()
            candidate=detector.update(row['tracks'],now,width,height,zone)
            elapsed=(time.perf_counter()-t)*1000
            if row['inspect']:
                positive=(row['inside_people']>=3) if zone else row['tracked_people']>=8
                old_hits=max(0,old_hits+(1 if positive else -1))
                if old_hits>=4:old_until=now+6;old_hits=0
            rows.append(dict(frame_index=row['frame_index'],source_seconds=now,
                legacy_active_same_video_clock=now<old_until,candidate=candidate,candidate_module_ms=elapsed))
            if row['frame_index']%10!=9:continue
            cap.set(cv2.CAP_PROP_POS_MSEC,now*1000);ok,image=cap.read()
            if not ok:raise RuntimeError('Cannot decode visualization')
            for p in row['tracks']:
                if p['id'] is None:continue
                x,y,X,Y=map(round,p['xyxy']);cv2.rectangle(image,(x,y),(X,Y),(0,220,0),2)
                cv2.putText(image,f"HUMAN ID:{p['id']}",(x,max(15,y-5)),0,.45,(0,220,0),1)
            for g in candidate['groups']:
                x,y,X,Y=map(round,g['xyxy']);color=(0,0,255) if g['level']=='HIGH' else (0,200,255)
                cv2.rectangle(image,(x,y),(X,Y),color,3)
                label=f"CROWD #{g['group_id']} {g['level'] if g['level']!='NORMAL' else 'CANDIDATE'} {g['people']} {'HOLD' if g['stale'] else ''}"
                cv2.putText(image,label,(x,max(25,y-12)),0,.6,color,2)
            cv2.putText(image,f"EXPERIMENT ONLY | IMAGE-SPACE UNCALIBRATED | {candidate['level']}",(12,30),0,.65,(0,200,255),2)
            cv2.putText(image,f"Unique people:{candidate['unique_people']} | old crowd:{now<old_until}",(12,60),0,.6,(0,200,255),2)
            cv2.imwrite(str(folder/f"overlay_{row['frame_index']:04d}.jpg"),image)
        cap.release()
        summary=dict(case=meta['case'],frames=len(rows),legacy_active_frames=sum(r['legacy_active_same_video_clock'] for r in rows),
            caution_frames=sum(r['candidate']['level']=='CAUTION' for r in rows),high_frames=sum(r['candidate']['level']=='HIGH' for r in rows),
            proposed_transitions=sum(len(r['candidate']['transitions']) for r in rows),
            frames_with_candidate_group=sum(bool(r['candidate']['groups']) for r in rows),
            mean_module_ms=statistics.mean(r['candidate_module_ms'] for r in rows),
            p95_module_ms=sorted(r['candidate_module_ms'] for r in rows)[int(.95*len(rows))-1])
        (folder/'comparison.json').write_text(json.dumps(dict(summary=summary,rows=rows),indent=2));summaries.append(summary)
    # Pure grouping cost stress check, not inference or UI FPS.
    stress=[]
    for count in [15,100,300]:
        tracks=[dict(id=i,confidence=.9,xyxy=[(i%20)*50,(i//20)*60,(i%20)*50+20,(i//20)*60+50]) for i in range(count)]
        d=CrowdRegions();times=[]
        for i in range(30):
            t=time.perf_counter();d.update(tracks,i/10,1920,1080);times.append((time.perf_counter()-t)*1000)
        stress.append(dict(people=count,mean_ms=statistics.mean(times),max_ms=max(times)))
    report=dict(config=asdict(CandidateConfig()),summaries=summaries,stress=stress,
        limitation='Cached real tracked boxes; no new YOLO inference or independent density/safety labels. Both policies use source video time. No GUI/PX4/events changed.',
        hashes=hashes,unchanged={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(ROOT))] for p in watched})
    (out/'summary.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':main()
