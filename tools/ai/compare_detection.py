"""Paired raw-detector comparisons. Never edits tracker settings or weights.

Counts/overlap candidates are diagnostics, not accuracy without ground truth.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import time

ROOT=Path(__file__).resolve().parents[2]


def overlap(a,b):
    ix=max(0,min(a[2],b[2])-max(a[0],b[0]))
    iy=max(0,min(a[3],b[3])-max(a[1],b[1]))
    inter=ix*iy
    aa=max(0,a[2]-a[0])*max(0,a[3]-a[1])
    bb=max(0,b[2]-b[0])*max(0,b[3]-b[1])
    return inter/max(aa+bb-inter,1e-9),inter/max(min(aa,bb),1e-9)


def inspect_boxes(boxes,shape):
    pairs=[]
    for i,a in enumerate(boxes):
        for j in range(i+1,len(boxes)):
            b=boxes[j]
            if a['class_id']!=b['class_id']:
                continue  # Fire and smoke are distinct, legitimately overlapping classes.
            iou,contain=overlap(a['xyxy'],b['xyxy'])
            if iou>=.5 or contain>=.9:
                pairs.append({'a':i,'b':j,'iou':iou,'containment':contain,'review':'candidate, not a confirmed duplicate'})
    large=[]
    for i,b in enumerate(boxes):
        x1,y1,x2,y2=b['xyxy']
        ratio=(x2-x1)*(y2-y1)/(shape[0]*shape[1])
        b['area_ratio']=ratio
        if ratio>=.25:
            large.append(i)
    return pairs,large


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--videos',nargs='+',type=Path,default=sorted((ROOT/'videos').glob('*.mp4')))
    parser.add_argument('--fractions',nargs='+',type=float,default=[.1,.5,.9])
    parser.add_argument('--device',default='cpu')
    parser.add_argument('--output',type=Path,default=ROOT/'diagnostics/ai_tuning/comparison')
    args=parser.parse_args()
    import cv2
    import torch
    from ultralytics import YOLO
    from ultralytics.utils import ROOT as UROOT
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    frames=[]
    for path in args.videos:
        path=path.resolve();cap=cv2.VideoCapture(str(path));count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));fps=cap.get(cv2.CAP_PROP_FPS)
        if not cap.isOpened():raise RuntimeError(f'Cannot decode {path}')
        for fraction in args.fractions:
            index=int((count-1)*fraction);cap.set(cv2.CAP_PROP_POS_FRAMES,index);ok,frame=cap.read()
            if not ok:raise RuntimeError(f'Cannot decode {path} frame {index}')
            key=f'{path.stem}_{index}'
            raw=out/'raw'/f'{key}.png';raw.parent.mkdir(exist_ok=True);cv2.imwrite(str(raw),frame)
            frames.append((key,frame,{'video':str(path),'frame':index,'seconds':index/fps,'raw':str(raw),'sha256':hashlib.sha256(frame.tobytes()).hexdigest()}))
        cap.release()
    settings={
        'person_vehicle': [('baseline',832,.35,.5),('lower_conf',832,.25,.5),('higher_resolution',1024,.35,.5),('stricter_nms',832,.35,.4),('faster_resolution',640,.35,.5)],
        'fire_smoke': [('baseline',640,.30,.7),('stricter_nms',640,.30,.5),('higher_resolution',832,.30,.7),('higher_conf',640,.40,.7)]}
    rows=[];models={};shapes=[]
    for group,configs in settings.items():
        filename='yolo11s.pt' if group=='person_vehicle' else 'fire_smoke_best.pt'
        path=ROOT/'models'/filename
        model=YOLO(str(path));models[group]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'names':model.names}
        shapes=[]
        def hook(module,inputs):
            if inputs and hasattr(inputs[0],'shape'):shapes.append(list(inputs[0].shape))
        handle=model.model.register_forward_pre_hook(hook)
        def predict(frame,cfg):
            _,size,conf,iou=cfg
            return model.predict(frame,device=args.device,imgsz=size,conf=conf,iou=iou,
                classes=[0,2,3,5,7] if group=='person_vehicle' else None,
                agnostic_nms=False,max_det=300,rect=True,verbose=False)[0]
        # Warm up each shape/config before timing. Do not time model loading.
        for cfg in configs:
            predict(frames[0][1],cfg);predict(frames[0][1],cfg)
        for fi,(key,frame,metadata) in enumerate(frames):
            # Rotate order to reduce fixed-order thermal/cache effects.
            order=configs[fi%len(configs):]+configs[:fi%len(configs)]
            for cfg in order:
                if args.device=='mps':torch.mps.synchronize()
                shapes.clear();start=time.perf_counter();result=predict(frame,cfg)
                if args.device=='mps':torch.mps.synchronize()
                elapsed=(time.perf_counter()-start)*1000
                boxes=[{'class_id':int(b.cls[0]),'class_name':result.names[int(b.cls[0])],
                        'confidence':float(b.conf[0]),'xyxy':b.xyxy[0].tolist()} for b in result.boxes]
                pairs,large=inspect_boxes(boxes,frame.shape)
                image_dir=out/group/cfg[0];image_dir.mkdir(parents=True,exist_ok=True)
                annotated=image_dir/f'{key}.jpg';cv2.imwrite(str(annotated),result.plot(line_width=1,font_size=9))
                row={**metadata,'key':key,'group':group,'setting':cfg[0],'imgsz':cfg[1],'conf':cfg[2],'iou':cfg[3],
                     'actual_tensor_shape':shapes[-1] if shapes else None,'wall_ms':elapsed,'speed_ms':result.speed,
                     'boxes':boxes,'overlap_candidates':pairs,'large_boxes':large,'annotated':str(annotated),
                     'effective_args':{k:getattr(model.predictor.args,k) for k in ['rect','agnostic_nms','max_det','conf','iou','imgsz']},
                     'end2end':bool(getattr(model.model,'end2end',False))}
                rows.append(row)
            print(group,key,'done',flush=True)
        handle.remove()
    summaries=[]
    for group,configs in settings.items():
        for cfg in configs:
            selected=[r for r in rows if r['group']==group and r['setting']==cfg[0]]
            times=[r['wall_ms'] for r in selected];counts=Counter(b['class_name'] for r in selected for b in r['boxes'])
            summaries.append({'group':group,'setting':cfg[0],'frames':len(selected),'counts':dict(counts),
                'mean_confidence':statistics.mean(b['confidence'] for r in selected for b in r['boxes']) if counts else None,
                'mean_wall_ms':statistics.mean(times),'median_wall_ms':statistics.median(times),
                'overlap_candidate_pairs':sum(len(r['overlap_candidates']) for r in selected),
                'large_boxes':sum(len(r['large_boxes']) for r in selected),
                'tensor_shapes':sorted(set(str(r['actual_tensor_shape']) for r in selected))})
    report={'device':args.device,'models':models,'tracker_config':(UROOT/'cfg/trackers/bytetrack.yaml').read_text(),
            'scope':'Same decoded frames, raw predict only; not ground-truth accuracy or end-to-end UI FPS.',
            'summaries':summaries,'rows':rows}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(summaries,indent=2),flush=True)


if __name__=='__main__':
    main()
