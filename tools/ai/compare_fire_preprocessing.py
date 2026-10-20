"""Paired photometric-only fire diagnostic, without importing/modifying the UI.

Reference patches are manually selected visible flame areas, not exhaustive
instance ground truth. Patch coverage is not mAP/whole-image recall.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

ROOT = Path(__file__).resolve().parents[2]
# Coordinates on a 640x360 reference view. Source-only visual review, not model boxes.
REFERENCES = {
 '14294878_1920_1080_24fps_65': [[296,244,317,267],[396,211,444,231]],
 '14294878_1920_1080_24fps_326': [[287,246,311,266],[190,205,211,226]],
 '14294878_1920_1080_24fps_586': [[317,226,346,242],[221,240,241,260],[448,162,480,188]],
 '14296766_1920_1080_24fps_83': [[418,228,434,239]],
 '14296766_1920_1080_24fps_419': [[370,228,386,239]],
 '14296766_1920_1080_24fps_754': [[361,231,377,240]],
 '14297188_1920_1080_24fps_59': [[324,231,336,242]],
 '14297188_1920_1080_24fps_299': [[287,184,332,207]],
 '14297188_1920_1080_24fps_539': [[288,163,320,192]],
 '4825783-hd_1920_1080_30fps_49': [[0,204,14,214],[306,177,326,193]],
 '4825783-hd_1920_1080_30fps_247': [[176,246,209,270],[466,243,487,264]],
 '4825783-hd_1920_1080_30fps_444': [[230,235,246,251],[420,225,445,243]],
}
VARIANTS = ['original', 'clahe_l2', 'gamma_0.7', 'gamma_1.4', 'contrast_1.2', 'darken_0.8']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    if args.repeats<1: parser.error('repeats must be positive')
    output=args.output.resolve()
    if output.exists(): parser.error('Use a new output directory')
    output.mkdir(parents=True)
    import cv2
    import numpy as np
    from ultralytics import YOLO
    tracker=ROOT/'legacy/drone_tracker.py'; weight=ROOT/'models/fire_smoke_best.pt'
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [tracker,weight]}
    model=YOLO(str(weight))
    source=ROOT/'diagnostics/ai_tuning/comparison/raw'
    keys=list(REFERENCES)+[p.stem for p in sorted(source.glob('7949326*.png'))]
    clahe=cv2.createCLAHE(clipLimit=2.0,tileGridSize=(8,8))
    luts={g:np.round(255*(np.arange(256)/255.)**g).astype(np.uint8) for g in [.7,1.4]}
    def transform(frame,name):
        if name=='original': return frame.copy()
        if name=='clahe_l2':
            lab=cv2.cvtColor(frame,cv2.COLOR_BGR2LAB)
            lab[:,:,0]=clahe.apply(lab[:,:,0])
            return cv2.cvtColor(lab,cv2.COLOR_LAB2BGR)
        if name.startswith('gamma_'): return cv2.LUT(frame,luts[float(name.split('_')[1])])
        if name=='contrast_1.2': return np.clip((frame.astype(np.float32)-128)*1.2+128,0,255).astype(np.uint8)
        return np.round(frame.astype(np.float32)*.8).astype(np.uint8)
    def predict(frame):
        return model.predict(frame,device='cpu',imgsz=640,conf=.30,iou=.7,
                             rect=True,agnostic_nms=False,max_det=300,verbose=False)[0]
    first=cv2.imread(str(source/(keys[0]+'.png')))
    predict(first);predict(first)
    rows=[];dataset=[]
    for index,key in enumerate(keys):
        frame=cv2.imread(str(source/(key+'.png')))
        if frame is None: raise RuntimeError(key)
        h,w=frame.shape[:2]; sx=w/640;sy=h/360
        rois=[[round(a*sx),round(b*sy),round(c*sx),round(d*sy)] for a,b,c,d in REFERENCES.get(key,[])]
        group=('night' if key.startswith('14297188') else 'sunset' if key.startswith('14296766')
               else 'negative_day' if key.startswith('7949326') else 'day')
        folder=output/key;folder.mkdir()
        gt=frame.copy()
        for ri,(x1,y1,x2,y2) in enumerate(rois):
            cv2.rectangle(gt,(x1,y1),(x2,y2),(255,0,255),2)
            cv2.putText(gt,f'R{ri}',(x1,max(20,y1-8)),0,.7,(255,0,255),2)
        cv2.imwrite(str(folder/'reference.png'),gt)
        raw_gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        metrics=[]
        for x1,y1,x2,y2 in rois:
            roi=raw_gray[y1:y2,x1:x2]
            margin=24; ax=max(0,x1-margin);ay=max(0,y1-margin);bx=min(w,x2+margin);by=min(h,y2+margin)
            ring=raw_gray[ay:by,ax:bx];mask=np.ones(ring.shape,bool)
            mask[y1-ay:y2-ay,x1-ax:x2-ax]=False
            metrics.append(dict(xyxy=[x1,y1,x2,y2],width_at_model_640=(x2-x1)*640/w,
                height_at_model_640=(y2-y1)*640/w,patch_area_ratio=roi.size/(h*w),
                gray_mean=float(roi.mean()),local_gray_delta=float(roi.mean()-ring[mask].mean()),
                saturation_gray_ge250=float((roi>=250).mean())))
        dataset.append(dict(key=key,group=group,reference_patches=metrics,
            mean_gray=float(raw_gray.mean()),p05_p95=np.percentile(raw_gray,[5,95]).tolist(),
            clipped_gray_ge250=float((raw_gray>=250).mean()),
            clipped_any_channel_ge250=float((frame.max(axis=2)>=250).mean()),
            source_sha256=hashlib.sha256(frame.tobytes()).hexdigest()))
        # Rotate variant order; repeat warmed inference on exactly the same pixels.
        order=VARIANTS[index%len(VARIANTS):]+VARIANTS[:index%len(VARIANTS)]
        panels={}
        for name in order:
            prep=[]
            for _ in range(args.repeats):
                t=time.perf_counter();corrected=transform(frame,name);prep.append((time.perf_counter()-t)*1000)
            predict(corrected)
            times=[]
            for _ in range(args.repeats):
                t=time.perf_counter();result=predict(corrected);times.append((time.perf_counter()-t)*1000)
            boxes=[dict(class_name=result.names[int(b.cls[0])],confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in result.boxes]
            fire=[b for b in boxes if b['class_name'].lower()=='fire']
            patches=[]
            for roi in rois:
                x1,y1,x2,y2=roi;area=(x2-x1)*(y2-y1);hits=[];ious=[]
                for b in fire:
                    a,c,d,e=b['xyxy'];inter=max(0,min(x2,d)-max(x1,a))*max(0,min(y2,e)-max(y1,c))
                    coverage=inter/area; iou=inter/max(1,area+(d-a)*(e-c)-inter)
                    if coverage>=.25:hits.append(b['confidence'])
                    ious.append(iou)
                patches.append(dict(xyxy=roi,covered_25pct=bool(hits),
                    ui_covered_25pct=any(c>=.35 for c in hits),
                    max_confidence=max(hits,default=0),max_iou=max(ious,default=0)))
            annotated=result.plot(line_width=2,font_size=12)
            for ri,(x1,y1,x2,y2) in enumerate(rois):
                cv2.rectangle(annotated,(x1,y1),(x2,y2),(255,0,255),2)
                cv2.putText(annotated,f'R{ri}',(x1,max(20,y1-8)),0,.7,(255,0,255),2)
            cv2.imwrite(str(folder/(name+'.jpg')),annotated)
            cv2.imwrite(str(folder/(name+'_input.png')),corrected)
            panel=cv2.resize(annotated,(640,360));cv2.putText(panel,name,(10,24),0,.7,(255,255,255),2);panels[name]=panel
            rows.append(dict(key=key,group=group,variant=name,boxes=boxes,patches=patches,
                inference_ms=statistics.median(times),inference_samples_ms=times,preprocessing_ms=statistics.median(prep),
                negative_fire_false_positives=len(fire) if group=='negative_day' else None))
        cv2.imwrite(str(folder/'comparison.jpg'),cv2.vconcat([cv2.hconcat([panels[n] for n in VARIANTS[i:i+3]]) for i in [0,3]]))
        print(key,'done',flush=True)
    summaries=[]
    for group in ['day','sunset','night','negative_day']:
        for name in VARIANTS:
            selected=[r for r in rows if r['group']==group and r['variant']==name]
            summaries.append(dict(group=group,variant=name,frames=len(selected),
                reference_patches=sum(len(r['patches']) for r in selected),
                covered_patches=sum(p['covered_25pct'] for r in selected for p in r['patches']),
                ui_covered_patches=sum(p['ui_covered_25pct'] for r in selected for p in r['patches']),
                fire_boxes=sum(b['class_name'].lower()=='fire' for r in selected for b in r['boxes']),
                mean_inference_ms=statistics.mean(r['inference_ms'] for r in selected),
                mean_preprocessing_ms=statistics.mean(r['preprocessing_ms'] for r in selected)))
    report=dict(settings=dict(imgsz=640,conf=.30,iou=.7,device='cpu',rect=True,agnostic_nms=False,max_det=300),
        definition='Partial manually selected visible-flame patches; hit=at least 25% patch covered by a FIRE box. Not exhaustive instance recall/mAP; unmatched predictions require review, not automatic false positives.',
        references_author='assistant visual review; not independently labeled',
        hashes=hashes,unchanged={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in hashes.items()},
        dataset=dataset,rows=rows,summaries=summaries)
    (output/'results.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(summaries,indent=2),flush=True)


if __name__=='__main__': main()
