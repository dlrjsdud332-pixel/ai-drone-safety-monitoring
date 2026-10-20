"""Ten-input paired raw-model experiment. Never imports the operational tracker.

Partial flame-patch diagnostics, not verified whole-image accuracy or mAP.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics
import time

from compare_fire_preprocessing import REFERENCES, ROOT

VARIANTS=['original','grayscale','gamma_1.2','gamma_1.4','gamma_1.6','clahe',
          'adaptive_threshold','otsu_threshold','sharpening','hsv_flame_emphasis']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=5)
    args=parser.parse_args()
    if args.repeats<2:parser.error('At least two repeats required')
    output=args.output.resolve()
    if output.exists():parser.error('Use a fresh output directory')
    output.mkdir(parents=True)
    import cv2
    import numpy as np
    from ultralytics import YOLO
    monitored=[ROOT/'legacy/drone_tracker.py',ROOT/'models/fire_smoke_best.pt',
               ROOT/'tools/gazebo/x500_camera.sdf',ROOT/'diagnostics/fire_dataset_v1/manifest.json']
    before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in monitored}
    source=ROOT/'diagnostics/ai_tuning/comparison/raw'
    keys=list(REFERENCES)+[p.stem for p in sorted(source.glob('7949326*.png'))]
    model=YOLO(str(ROOT/'models/fire_smoke_best.pt'))
    clahe=cv2.createCLAHE(clipLimit=2,tileGridSize=(8,8))
    luts={g:np.round(255*(np.arange(256)/255)**g).astype(np.uint8) for g in [1.2,1.4,1.6]}
    def transform(frame,name):
        if name=='original':return frame.copy()
        if name.startswith('gamma_'):return cv2.LUT(frame,luts[float(name.split('_')[1])])
        if name=='clahe':
            lab=cv2.cvtColor(frame,cv2.COLOR_BGR2LAB);lab[:,:,0]=clahe.apply(lab[:,:,0])
            return cv2.cvtColor(lab,cv2.COLOR_LAB2BGR)
        if name=='sharpening':return cv2.addWeighted(frame,2,cv2.GaussianBlur(frame,(0,0),1),-1,0)
        if name=='hsv_flame_emphasis':
            hsv=cv2.cvtColor(frame,cv2.COLOR_BGR2HSV)
            mask=(((hsv[:,:,0]<=35)|(hsv[:,:,0]>=170))&(hsv[:,:,1]>=80)&(hsv[:,:,2]>=80))
            adjusted=frame.astype(np.float32)*.65
            adjusted[mask]=frame[mask].astype(np.float32)*np.array([.9,1.05,1.15],np.float32)
            return np.clip(adjusted,0,255).astype(np.uint8)
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        if name=='adaptive_threshold':gray=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY,31,5)
        if name=='otsu_threshold':gray=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
        return cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR)
    def predict(frame):
        return model.predict(frame,device='cpu',imgsz=640,conf=.30,iou=.70,
            rect=True,agnostic_nms=False,max_det=300,verbose=False)[0]
    rows=[];dataset=[]
    for fi,key in enumerate(keys):
        raw=cv2.imread(str(source/(key+'.png')))
        if raw is None:raise RuntimeError(key)
        h,w=raw.shape[:2];gray_mean=float(cv2.cvtColor(raw,cv2.COLOR_BGR2GRAY).mean())
        group='night' if key.startswith('14297188') else 'sunset' if key.startswith('14296766') else 'negative_day' if key.startswith('7949326') else 'day'
        patches=[[x*w/640,y*h/360,X*w/640,Y*h/360] for x,y,X,Y in REFERENCES.get(key,[])]
        dataset.append(dict(key=key,group=group,mean_gray=gray_mean,raw_sha256=hashlib.sha256(raw.tobytes()).hexdigest(),reference_patches=patches))
        folder=output/key;folder.mkdir();transformed={};prep={};times={};results={}
        for name in VARIANTS:
            measurements=[]
            for _ in range(args.repeats):
                t=time.perf_counter();image=transform(raw,name);measurements.append((time.perf_counter()-t)*1000)
            if image.dtype!=np.uint8 or image.shape!=raw.shape:raise ValueError('Expected same-sized three-channel uint8 BGR')
            transformed[name]=image;prep[name]=statistics.mean(measurements);times[name]=[]
            predict(image)  # Each transformed input is warmed outside timing.
        for repeat in range(args.repeats):
            shift=(fi+repeat)%len(VARIANTS)
            for name in VARIANTS[shift:]+VARIANTS[:shift]:
                t=time.perf_counter();result=predict(transformed[name]);times[name].append((time.perf_counter()-t)*1000);results[name]=result
        panels={};patch_panels={i:[] for i in range(len(patches))}
        for name in VARIANTS:
            result=results[name]
            boxes=[dict(name=result.names[int(b.cls[0])],confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in result.boxes]
            flame=[b for b in boxes if b['name']=='fire'];patch_results=[]
            for pi,(x,y,X,Y) in enumerate(patches):
                candidates=[]
                for b in flame:
                    a,c,d,e=b['xyxy'];coverage=max(0,min(X,d)-max(x,a))*max(0,min(Y,e)-max(y,c))/((X-x)*(Y-y))
                    if coverage>=.25:candidates.append(b['confidence'])
                patch_results.append(dict(index=pi,covered_raw=bool(candidates),covered_display=any(c>=.35 for c in candidates),
                    confidence=max(candidates,default=0),width_at_640=(X-x)*640/w,height_at_640=(Y-y)*640/w))
            smoke_threshold=.62 if gray_mean<70 else .50
            visible=[b for b in boxes if b['confidence']>=(.35 if b['name']=='fire' else smoke_threshold)]
            counts=Counter(b['name'] for b in boxes);display_counts=Counter(b['name'] for b in visible)
            rows.append(dict(key=key,group=group,variant=name,boxes=boxes,patches=patch_results,
                fire_boxes=counts['fire'],smoke_boxes=counts['smoke'],display_fire_boxes=display_counts['fire'],display_smoke_boxes=display_counts['smoke'],
                smoke_display_threshold=smoke_threshold,inference_mean_ms=statistics.mean(times[name]),inference_samples_ms=times[name],
                preprocessing_mean_ms=prep[name],
                negative_control_fp_fire=counts['fire'] if group=='negative_day' else None,
                negative_control_fp_smoke=counts['smoke'] if group=='negative_day' else None))
            annotated=result.plot(line_width=2,font_size=12)
            for pi,(x,y,X,Y) in enumerate(patches):
                cv2.rectangle(annotated,(round(x),round(y)),(round(X),round(Y)),(255,0,255),2)
                cv2.putText(annotated,f'R{pi}',(round(x),max(25,round(y)-8)),0,.7,(255,0,255),2)
                a=max(0,round(x)-45);c=max(0,round(y)-45);d=min(w,round(X)+45);e=min(h,round(Y)+45)
                crop=cv2.resize(annotated[c:e,a:d],(320,220));cv2.putText(crop,name,(5,20),0,.5,(255,255,255),1)
                patch_panels[pi].append(crop)
            cv2.imwrite(str(folder/(name+'_input.jpg')),transformed[name],[cv2.IMWRITE_JPEG_QUALITY,93])
            cv2.imwrite(str(folder/(name+'_detections.jpg')),annotated,[cv2.IMWRITE_JPEG_QUALITY,93])
            panel=cv2.resize(annotated,(640,360));cv2.putText(panel,name,(8,24),0,.65,(255,255,255),2);panels[name]=panel
        cv2.imwrite(str(folder/'comparison.jpg'),cv2.vconcat([cv2.hconcat([panels[n] for n in VARIANTS[i:i+2]]) for i in range(0,10,2)]))
        for pi,tiles in patch_panels.items():
            cv2.imwrite(str(folder/f'patch_R{pi}.jpg'),cv2.vconcat([cv2.hconcat(tiles[i:i+5]) for i in [0,5]]))
        print(key,'complete',flush=True)
    summaries=[]
    for group in ['day','sunset','night','negative_day','all']:
        for name in VARIANTS:
            selected=[r for r in rows if r['variant']==name and (group=='all' or r['group']==group)]
            summaries.append(dict(group=group,variant=name,frames=len(selected),
                fire_boxes=sum(r['fire_boxes'] for r in selected),smoke_boxes=sum(r['smoke_boxes'] for r in selected),
                display_fire_boxes=sum(r['display_fire_boxes'] for r in selected),display_smoke_boxes=sum(r['display_smoke_boxes'] for r in selected),
                reference_patches=sum(len(r['patches']) for r in selected),
                covered_patches_raw=sum(p['covered_raw'] for r in selected for p in r['patches']),
                covered_patches_display=sum(p['covered_display'] for r in selected for p in r['patches']),
                negative_fp_fire=sum(r['negative_control_fp_fire'] or 0 for r in selected) if group in ['negative_day','all'] else None,
                negative_fp_smoke=sum(r['negative_control_fp_smoke'] or 0 for r in selected) if group in ['negative_day','all'] else None,
                inference_mean_ms=statistics.mean(r['inference_mean_ms'] for r in selected),
                preprocessing_mean_ms=statistics.mean(r['preprocessing_mean_ms'] for r in selected)))
    with (output/'summary.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=summaries[0].keys());writer.writeheader();writer.writerows(summaries)
    report=dict(settings=dict(conf=.30,iou=.70,imgsz=640,device='cpu',rect=True,agnostic_nms=False,max_det=300),
        repeats=args.repeats,dataset=dataset,rows=rows,summaries=summaries,
        scope='Partial visually selected flame-patch coverage, not human-verified full labels. Negative-control FP counts limited to three previously visually inspected no-fire/no-smoke frames. Unmatched positive-image boxes are not automatically FP. Display counts apply confidence only, not UI sky/cloud filters or alarm persistence.',
        hashes=before,unchanged={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()==before[str(p.relative_to(ROOT))] for p in monitored})
    (output/'results.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(summaries,indent=2),flush=True)


if __name__=='__main__':main()
