"""Diagnostic HSV-proposed original-color crop retries; no production imports.

Paired partial-region checks and paced latest-frame file-input throughput.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import threading
import time

from compare_fire_preprocessing import REFERENCES, ROOT

METHODS=['original','full_hsv','roi_k1','roi_k2']
# Visually checked FIRE-free subregions, not exhaustive full-frame labels.
CONTROLS={
 '14294878_1920_1080_24fps_65':[[1450,700,1910,1040]],
 '4825783-hd_1920_1080_30fps_49':[[1450,650,1880,950]],
 '14296766_1920_1080_24fps_83':[[1500,0,1910,180]],
 '14296766_1920_1080_24fps_419':[[1400,0,1850,150]],
 '14296766_1920_1080_24fps_754':[[1540,0,1910,160]],
}


def proposals(frame,limit):
    import cv2
    import numpy as np
    h,w=frame.shape[:2];small=cv2.resize(frame,(640,360),interpolation=cv2.INTER_AREA)
    hsv=cv2.cvtColor(small,cv2.COLOR_BGR2HSV)
    mask=((((hsv[:,:,0]<=35)|(hsv[:,:,0]>=170))&(hsv[:,:,1]>=80)&(hsv[:,:,2]>=80)).astype(np.uint8)*255)
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    count,labels,stats,centers=cv2.connectedComponentsWithStats(mask,8)
    ranked=[]
    for i in range(1,count):
        x,y,bw,bh,area=map(int,stats[i])
        if area<2 or area>1152 or bw>160 or bh>120:continue
        values=hsv[y:y+bh,x:x+bw,2];component=labels[y:y+bh,x:x+bw]==i
        bright=float(values[component].mean())
        ring=hsv[max(0,y-6):min(360,y+bh+6),max(0,x-6):min(640,x+bw+6),2]
        score=area**.5*(bright/255)*(1+max(0,bright-float(ring.mean()))/128)
        cx,cy=centers[i];side=round(min(640,max(192,4*max(bw*w/640,bh*h/360))))
        X=max(0,min(w-side,round(cx*w/640-side/2)));Y=max(0,min(h-side,round(cy*h/360-side/2)))
        ranked.append(dict(xyxy=[X,Y,X+side,Y+side],score=score,component_area_at_640=area))
    ranked.sort(key=lambda r:-r['score']);selected=[]
    for candidate in ranked:
        x,y,X,Y=candidate['xyxy']
        if any(abs((p['xyxy'][0]+p['xyxy'][2])/2-(x+X)/2)<(X-x)*.4 and abs((p['xyxy'][1]+p['xyxy'][3])/2-(y+Y)/2)<(Y-y)*.4 for p in selected):continue
        selected.append(candidate)
        if len(selected)>=limit:break
    return selected,len(ranked)


def emphasize(frame):
    import cv2
    import numpy as np
    hsv=cv2.cvtColor(frame,cv2.COLOR_BGR2HSV)
    mask=(((hsv[:,:,0]<=35)|(hsv[:,:,0]>=170))&(hsv[:,:,1]>=80)&(hsv[:,:,2]>=80))
    out=frame.astype(np.float32)*.65
    out[mask]=frame[mask].astype(np.float32)*np.array([.9,1.05,1.15],np.float32)
    return np.clip(out,0,255).astype(np.uint8)


def fuse(boxes):
    # Same-class NMS at the existing IoU=.70, only in diagnostic output fusion.
    from prepare_fire_dataset import iou
    remaining=sorted(boxes,key=lambda b:-b['confidence']);kept=[]
    for box in remaining:
        if any(b['name']==box['name'] and iou(b['xyxy'],box['xyxy'])>.70 for b in kept):continue
        kept.append(box)
    return kept


class Pipeline:
    def __init__(self):
        from ultralytics import YOLO
        self.model=YOLO(str(ROOT/'models/fire_smoke_best.pt'))

    def predict(self,frame,origin):
        result=self.model.predict(frame,device='cpu',imgsz=640,conf=.30,iou=.70,rect=True,agnostic_nms=False,max_det=300,verbose=False)[0]
        return [dict(name=result.names[int(b.cls[0])],confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist(),origin=origin) for b in result.boxes]

    def run(self,frame,method):
        import cv2
        start=time.perf_counter();cpu=time.process_time();roi=[];proposal_ms=0;resize_ms=0;extra_ms=0
        if method=='full_hsv':
            t=time.perf_counter();corrected=emphasize(frame);proposal_ms=(time.perf_counter()-t)*1000
            t=time.perf_counter();boxes=self.predict(corrected,'full_hsv');base_ms=(time.perf_counter()-t)*1000
            total_candidates=0;raw_crops=[]
        else:
            t=time.perf_counter();boxes=self.predict(frame,'original');base_ms=(time.perf_counter()-t)*1000
            total_candidates=0;raw_crops=[]
            if method.startswith('roi_'):
                limit=int(method[-1]);t=time.perf_counter();roi,total_candidates=proposals(frame,limit);proposal_ms=(time.perf_counter()-t)*1000
                for region in roi:
                    x,y,X,Y=region['xyxy'];t=time.perf_counter()
                    crop=cv2.resize(frame[y:Y,x:X],(640,640),interpolation=cv2.INTER_LINEAR);resize_ms+=(time.perf_counter()-t)*1000
                    t=time.perf_counter();found=self.predict(crop,'roi_retry');extra_ms+=(time.perf_counter()-t)*1000
                    for box in found:
                        if box['name']!='fire':continue  # Preserve original SMOKE results.
                        a,b,c,d=box['xyxy'];box['xyxy']=[x+a*(X-x)/640,y+b*(Y-y)/640,x+c*(X-x)/640,y+d*(Y-y)/640]
                        raw_crops.append(box)
                boxes=fuse(boxes+raw_crops)
        return dict(boxes=boxes,roi=roi,total_color_components=total_candidates,crop_fire_boxes=raw_crops,
            wall_ms=(time.perf_counter()-start)*1000,cpu_ms=(time.process_time()-cpu)*1000,
            base_inference_ms=base_ms,proposal_or_emphasis_ms=proposal_ms,resize_ms=resize_ms,retry_inference_ms=extra_ms)


def hits(boxes,patches,threshold=.35):
    answer=[]
    for x,y,X,Y in patches:
        conf=[]
        for b in boxes:
            if b['name']!='fire' or b['confidence']<threshold:continue
            a,c,d,e=b['xyxy'];coverage=max(0,min(X,d)-max(x,a))*max(0,min(Y,e)-max(y,c))/((X-x)*(Y-y))
            if coverage>=.25:conf.append(b['confidence'])
        answer.append(max(conf,default=0))
    return answer


def paced(pipeline,video,method,count,hz):
    """Actual paced decode/publish thread with a bounded latest-frame buffer."""
    import cv2
    cap=cv2.VideoCapture(str(video));fps=cap.get(cv2.CAP_PROP_FPS)
    lock=threading.Lock();latest=[None];done=threading.Event();started=threading.Event();published=[];error=[]
    def produce():
        try:
            if not cap.isOpened():raise RuntimeError('Cannot decode stream input')
            zero=time.perf_counter();started.set();native=-1
            for seq in range(count):
                delay=zero+seq/hz-time.perf_counter()
                if delay>0:time.sleep(delay)
                target=round(seq*fps/hz)
                while native<target-1:
                    if not cap.grab():raise RuntimeError('Input ended during grab')
                    native+=1
                ok,frame=cap.read();native+=1
                if not ok:raise RuntimeError('Input ended during read')
                stamp=time.perf_counter()
                with lock:latest[0]=(seq,stamp,frame)
                published.append(stamp)
        except Exception as exc:error.append(str(exc))
        finally:cap.release();done.set();started.set()
    thread=threading.Thread(target=produce);thread.start();started.wait()
    begin=time.perf_counter();seen=-1;rows=[]
    while True:
        with lock:item=latest[0]
        if item is not None and item[0]!=seen:
            seen,stamp,frame=item;result=pipeline.run(frame,method)
            rows.append(dict(sequence=seen,wall_ms=result['wall_ms'],cpu_ms=result['cpu_ms'],
                source_to_result_ms=(time.perf_counter()-stamp)*1000,rois=len(result['roi'])))
        elif done.is_set():break
        else:time.sleep(.001)
    elapsed=time.perf_counter()-begin;thread.join()
    return dict(video=video.name,method=method,published=len(published),processed=len(rows),
        dropped=len(published)-len(rows),elapsed_seconds=elapsed,measured_processed_fps=len(rows)/elapsed,
        measured_publish_fps=(len(published)-1)/(published[-1]-published[0]) if len(published)>1 else None,
        mean_compute_ms=statistics.mean(x['wall_ms'] for x in rows) if rows else None,
        p95_compute_ms=sorted(x['wall_ms'] for x in rows)[max(0,int(len(rows)*.95)-1)] if rows else None,
        errors=error,rows=rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3);parser.add_argument('--stream-frames',type=int,default=30)
    args=parser.parse_args();out=args.output.resolve()
    if out.exists():parser.error('Use a fresh output directory')
    if args.repeats<1 or args.stream_frames<2:parser.error('Invalid measurement counts')
    out.mkdir(parents=True)
    import cv2
    import numpy as np
    watched=[ROOT/'legacy/drone_tracker.py',ROOT/'models/fire_smoke_best.pt',ROOT/'tools/gazebo/x500_camera.sdf',ROOT/'diagnostics/fire_dataset_v1/manifest.json']
    before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    pipeline=Pipeline();source=ROOT/'diagnostics/ai_tuning/comparison/raw'
    keys=list(REFERENCES)+[p.stem for pattern in ['7949326*.png','7949436*.png'] for p in sorted(source.glob(pattern))]
    rows=[]
    for fi,key in enumerate(keys):
        frame=cv2.imread(str(source/(key+'.png')));h,w=frame.shape[:2]
        patches=[[x*w/640,y*h/360,X*w/640,Y*h/360] for x,y,X,Y in REFERENCES.get(key,[])]
        group='night' if key.startswith('14297188') else 'sunset' if key.startswith('14296766') else 'negative' if key.startswith('7949') else 'day_forest' if key.startswith('4825') else 'day_field'
        folder=out/key;folder.mkdir();panels={}
        for method in METHODS:pipeline.run(frame,method)
        measurements={method:[] for method in METHODS}
        for repeat in range(args.repeats):
            shift=(fi+repeat)%len(METHODS)
            for method in METHODS[shift:]+METHODS[:shift]:measurements[method].append(pipeline.run(frame,method))
        for method,results in measurements.items():
            last=results[-1];boxes=last['boxes'];fire=[b for b in boxes if b['name']=='fire' and b['confidence']>=.35]
            patch_hits=hits(boxes,patches);controls=CONTROLS.get(key,[])
            control_fp=[b for b in fire if any(x<=(b['xyxy'][0]+b['xyxy'][2])/2<=X and y<=(b['xyxy'][1]+b['xyxy'][3])/2<=Y for x,y,X,Y in controls)]
            row=dict(key=key,group=group,method=method,patch_confidences=patch_hits,patches=patches,
                region_hits=sum(c>0 for c in patch_hits),roi_count=len(last['roi']),negative_fire_fp=len(fire) if group=='negative' else None,
                fire_free_window_fp=len(control_fp),control_windows=controls,fire_boxes=len(fire),smoke_boxes=sum(b['name']=='smoke' for b in boxes),**last)
            for metric in ['wall_ms','cpu_ms','base_inference_ms','proposal_or_emphasis_ms','resize_ms','retry_inference_ms']:
                row[metric]=statistics.mean(r[metric] for r in results)
            row['timing_samples_ms']=[r['wall_ms'] for r in results];rows.append(row)
            im=frame.copy()
            for p in last['roi']:
                x,y,X,Y=p['xyxy'];cv2.rectangle(im,(x,y),(X,Y),(255,255,0),2)
            for b in fire:
                x,y,X,Y=map(round,b['xyxy']);cv2.rectangle(im,(x,y),(X,Y),(0,0,255),2);cv2.putText(im,f"FIRE {b['confidence']:.2f}",(x,max(20,y-5)),0,.6,(0,0,255),2)
            for i,(x,y,X,Y) in enumerate(patches):cv2.rectangle(im,(round(x),round(y)),(round(X),round(Y)),(255,0,255),2)
            for x,y,X,Y in controls:cv2.rectangle(im,(x,y),(X,Y),(0,255,0),3)
            cv2.imwrite(str(folder/(method+'.jpg')),im)
            panel=cv2.resize(im,(640,360));cv2.putText(panel,method,(10,26),0,.7,(255,255,255),2);panels[method]=panel
            if method=='roi_k2':
                for i,p in enumerate(last['roi']):
                    x,y,X,Y=p['xyxy'];cv2.imwrite(str(folder/f'candidate_{i}_original_color.jpg'),cv2.resize(frame[y:Y,x:X],(640,640)))
        cv2.imwrite(str(folder/'comparison.jpg'),cv2.vconcat([cv2.hconcat([panels[m] for m in METHODS[i:i+2]]) for i in [0,2]]))
        print('paired',key,flush=True)
    write=lambda name,obj:(out/name).write_text(json.dumps(obj,indent=2))
    write('paired.json',dict(rows=rows,controls=CONTROLS,partial_reference_only=True,hashes=before))
    streams=[]
    for stem in ['14294878_1920_1080_24fps','4825783-hd_1920_1080_30fps','14296766_1920_1080_24fps','7949326-hd_1920_1080_30fps']:
        for method in METHODS:
            result=paced(pipeline,ROOT/'videos'/(stem+'.mp4'),method,args.stream_frames,10)
            streams.append(result);write('stream.json',streams)
            print('stream',stem,method,result['processed'],result['dropped'],round(result['measured_processed_fps'],2),flush=True)
    write('preservation.json',{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()==before[str(p.relative_to(ROOT))] for p in watched})


if __name__=='__main__':main()
