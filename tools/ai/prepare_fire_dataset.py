"""Build a quarantined review set; predict and score only explicitly verified labels.

No training, downloads, production imports, or operational changes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

from compare_fire_preprocessing import REFERENCES, ROOT

SOURCES = {
 '14294878_1920_1080_24fps': ('day','field_fire_related','test',True),
 '14296766_1920_1080_24fps': ('sunset','field_fire_related','test',True),
 '14297188_1920_1080_24fps': ('night','field_fire_related','test',True),
 '4825783-hd_1920_1080_30fps': ('day','forest_fire','val',True),
 '7949326-hd_1920_1080_30fps': ('day','crowd_related','test',False),
 '7949436-hd_1920_1080_30fps': ('day','crowd_related','test',False),
}


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value): path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def build(out):
    import cv2
    import numpy as np
    if out.exists(): raise SystemExit('Use a new dataset directory')
    for sub in ['images','labels_draft','labels_reviewed','review_sheets']:
        (out/sub).mkdir(parents=True)
    records=[]; sources=[]
    for stem,(period,group,split,positive) in SOURCES.items():
        video=ROOT/'videos'/(stem+'.mp4');cap=cv2.VideoCapture(str(video))
        if not cap.isOpened(): raise RuntimeError(str(video))
        n=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));fps=cap.get(cv2.CAP_PROP_FPS);tiles=[]
        sources.append(dict(video=str(video),sha256=digest(video),group=group,
            split=split,period=period,frames=n,fps=fps,license='not documented',
            relation_status='conservative grouping; original provenance not established'))
        for fraction in [.1,.25,.5,.75,.9]:
            index=int((n-1)*fraction);key=f'{stem}_{index}'
            cap.set(cv2.CAP_PROP_POS_FRAMES,index);ok,image=cap.read()
            if not ok: raise RuntimeError(key)
            h,w=image.shape[:2];path=out/'images'/(key+'.png');cv2.imwrite(str(path),image)
            boxes=REFERENCES.get(key,[]);lines=[];preview=image.copy()
            for x,y,X,Y in boxes:
                # Original class order retained: smoke=0, fire=1.
                lines.append(f'1 {(x+X)/1280:.8f} {(y+Y)/720:.8f} {(X-x)/640:.8f} {(Y-y)/360:.8f}')
                cv2.rectangle(preview,(round(x*w/640),round(y*h/360)),(round(X*w/640),round(Y*h/360)),(255,0,255),3)
            label=out/'labels_draft'/(key+'.txt');label.write_text('\n'.join(lines)+('\n' if lines else ''))
            gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY);small=cv2.resize(gray,(9,8))
            bits=(small[:,1:]>small[:,:-1]).flatten();dh=sum(int(b)<<i for i,b in enumerate(bits))
            records.append(dict(id=key,image=str(path.relative_to(out)),image_sha256=digest(path),
                draft_label=str(label.relative_to(out)),label=f'labels_reviewed/{key}.txt',
                source_group=group,split=split,period=period,source_video=str(video),frame=index,seconds=index/fps,
                width=w,height=h,dhash=f'{dh:016x}',mean_gray=float(gray.mean()),
                expected_fire=positive,expected_fire_is_verified=False,
                priority_tags=(['small_flame','smoke_occlusion','low_contrast'] if period=='day' and positive else ['negative_candidate'] if not positive else [period]),
                priority_tags_are='scene-level collection targets, not verified per-frame labels',
                draft_boxes=len(boxes),draft_scope='partial fire patches only; smoke unlabeled; empty file does NOT certify negative',
                review=dict(status='pending',reviewer='',complete_for_classes=[],fire_present=None,
                            image_sha256='',label_sha256='',notes='')))
            tile=cv2.resize(preview,(640,360));cv2.putText(tile,f'{key} DRAFT',(5,23),0,.47,(255,255,255),1);tiles.append(tile)
        cap.release()
        blank=np.zeros_like(tiles[0]);padded=tiles+[blank]
        cv2.imwrite(str(out/'review_sheets'/(stem+'.jpg')),cv2.vconcat([cv2.hconcat(padded[:3]),cv2.hconcat(padded[3:])]))
    leaks=[]
    for i,a in enumerate(records):
        for b in records[i+1:]:
            if a['split']==b['split']:continue
            dist=(int(a['dhash'],16)^int(b['dhash'],16)).bit_count()
            if a['image_sha256']==b['image_sha256'] or dist<=6:
                leaks.append(dict(a=a['id'],b=b['id'],dhash_distance=dist,status='unresolved'))
    write(out/'manifest.json',dict(schema_version=1,names={'0':'smoke','1':'fire'},
        annotation_policy='Visible flame envelope, not smoke halo; exhaustive both classes; ambiguous frames remain pending.',
        sources=sources,records=records,cross_split_similarity_candidates=leaks,
        split_policy='No train data. Related field clips grouped together; related crowd clips grouped together. Video-level assignment fixed before prediction. Near-duplicate audit is heuristic, not provenance proof.'))
    write(out/'inventory.json',dict(frames=len(records),by_period=dict(Counter(x['period'] for x in records)),
        by_split=dict(Counter(x['split'] for x in records)),draft_boxes=sum(x['draft_boxes'] for x in records),
        frames_with_draft_boxes=sum(bool(x['draft_boxes']) for x in records),verified_frames=0,
        cross_split_similarity_candidates=leaks))


def verified(out,manifest):
    if any(x.get('status')!='resolved' for x in manifest['cross_split_similarity_candidates']):
        raise ValueError('Resolve cross-split similarity candidates before evaluation/export')
    groups={};hashes={};approved=[]
    for r in manifest['records']:
        if r['source_group'] in groups and groups[r['source_group']]!=r['split']:
            raise ValueError('Source group split leakage')
        groups[r['source_group']]=r['split']
        if r['image_sha256'] in hashes and hashes[r['image_sha256']]!=r['split']:
            raise ValueError('Exact cross-split duplicate')
        hashes[r['image_sha256']]=r['split']
        v=r['review']
        if v['status']!='verified': continue
        if not v['reviewer'].strip() or set(v['complete_for_classes'])!={'smoke','fire'} or not isinstance(v['fire_present'],bool):
            raise ValueError(f'{r["id"]}: incomplete verification')
        image=out/r['image'];label=out/r['label']
        if digest(image)!=v['image_sha256'] or digest(image)!=r['image_sha256'] or digest(label)!=v['label_sha256']:
            raise ValueError(f'{r["id"]}: reviewed content changed')
        boxes=[]
        for line in label.read_text().splitlines():
            fields=line.split()
            if len(fields)!=5: raise ValueError('Expected YOLO class cx cy width height')
            cls,cx,cy,w,h=map(float,fields)
            if cls not in [0,1] or not (0<w<=1 and 0<h<=1 and w/2-1e-6<=cx<=1-w/2+1e-6 and h/2-1e-6<=cy<=1-h/2+1e-6):
                raise ValueError(f'Invalid YOLO box: {line}')
            boxes.append((int(cls),[(cx-w/2)*r['width'],(cy-h/2)*r['height'],(cx+w/2)*r['width'],(cy+h/2)*r['height']]))
        if any(c==1 for c,b in boxes)!=v['fire_present']: raise ValueError('Verified fire presence contradicts labels')
        approved.append((r,boxes))
    return approved


def iou(a,b):
    inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return inter/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)


def baseline(out):
    from ultralytics import YOLO
    manifest=json.loads((out/'manifest.json').read_text());weight=ROOT/'models/fire_smoke_best.pt'
    model=YOLO(str(weight));rows=[]
    for r in manifest['records']:
        image=out/r['image']
        if digest(image)!=r['image_sha256']:raise ValueError('Image content changed')
        t=time.perf_counter();result=model.predict(str(image),device='cpu',imgsz=640,conf=.30,iou=.70,rect=True,agnostic_nms=False,max_det=300,verbose=False)[0]
        rows.append(dict(id=r['id'],image_sha256=r['image_sha256'],wall_ms=(time.perf_counter()-t)*1000,
            boxes=[dict(name=result.names[int(b.cls[0])],confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in result.boxes]))
    write(out/'baseline_predictions.json',dict(weight=str(weight),weight_sha256=digest(weight),
        settings=dict(conf=.30,iou=.70,imgsz=640,device='cpu'),
        scope='Unscored candidate inference; timings include startup on first frame. Not accuracy.',rows=rows))


def evaluate(out):
    manifest=json.loads((out/'manifest.json').read_text());approved=verified(out,manifest)
    predictions=json.loads((out/'baseline_predictions.json').read_text());lookup={r['id']:r for r in predictions['rows']}
    metrics=[]
    for split in ['val','test']:
        for period in ['day','sunset','night']:
            for threshold in [.30,.35]:
                tp=fp=fn=negative=negative_with_fp=frames=positive_frames=missed_positive_frames=0
                for r,labels in approved:
                    if r['period']!=period or r['split']!=split:continue
                    row=lookup[r['id']]
                    if row['image_sha256']!=r['image_sha256']:raise ValueError('Prediction/image mismatch')
                    frames+=1;gt=[b for c,b in labels if c==1];used=set()
                    pred=sorted([b for b in row['boxes'] if b['name'].lower()=='fire' and b['confidence']>=threshold],key=lambda b:-b['confidence'])
                    matched=0
                    for b in pred:
                        candidates=[(iou(b['xyxy'],g),j) for j,g in enumerate(gt) if j not in used]
                        score,j=max(candidates,default=(0,-1))
                        if score>=.5:used.add(j);tp+=1;matched+=1
                        else:fp+=1
                    fn+=len(gt)-len(used)
                    if gt:positive_frames+=1;missed_positive_frames+=matched==0
                    else:negative+=1;negative_with_fp+=bool(pred)
                metrics.append(dict(split=split,period=period,confidence=threshold,frames=frames,tp=tp,fp=fp,fn=fn,
                    instance_miss_rate=fn/(tp+fn) if tp+fn else None,
                    fire_precision=tp/(tp+fp) if tp+fp else None,
                    positive_frames=positive_frames,missed_positive_frames=missed_positive_frames,
                    positive_frame_miss_rate=missed_positive_frames/positive_frames if positive_frames else None,
                    negative_frames=negative,negative_frames_with_fire=negative_with_fp,
                    negative_frame_false_alarm_rate=negative_with_fp/negative if negative else None,
                    false_boxes_per_image=fp/frames if frames else None))
    write(out/'evaluation.json',dict(status='scored' if approved else 'insufficient_verified_ground_truth',
        verified_frames=len(approved),excluded_frames=len(manifest['records'])-len(approved),
        matching='confidence-ordered one-to-one same-class IoU>=0.5; not AP/mAP',
        limitation='Framewise FIRE only; no temporal/event alarm score. Prior checkpoint-training overlap unknown.',metrics=metrics))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['build','baseline','evaluate','export'])
    parser.add_argument('--dataset',type=Path,required=True);args=parser.parse_args();out=args.dataset.resolve()
    if args.action=='build':build(out)
    elif args.action=='baseline':baseline(out)
    elif args.action=='evaluate':evaluate(out)
    else:
        import shutil
        manifest=json.loads((out/'manifest.json').read_text());approved=verified(out,manifest)
        if not approved:raise SystemExit('No verified frames; YOLO export refused')
        dest=out/'approved_yolo'
        if dest.exists():raise SystemExit('Export exists; use a fresh destination dataset')
        lists={s:[] for s in ['train','val','test']}
        for r,boxes in approved:
            split=r['split']
            for sub in ['images','labels']:(dest/sub/split).mkdir(parents=True,exist_ok=True)
            image=dest/'images'/split/(r['id']+'.png');shutil.copy2(out/r['image'],image)
            shutil.copy2(out/r['label'],dest/'labels'/split/(r['id']+'.txt'));lists[split].append(str(image))
        for split,paths in lists.items():(dest/(split+'.txt')).write_text('\n'.join(paths)+'\n' if paths else '')
        (dest/'data.yaml').write_text(f'path: {json.dumps(str(dest))}\ntrain: train.txt\nval: val.txt\ntest: test.txt\nnames:\n  0: smoke\n  1: fire\n')
    print(args.action,'finished:',out)


if __name__=='__main__': main()
