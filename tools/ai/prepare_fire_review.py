"""Offline human review editor, explicit result import, and verified-only scoring."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

from prepare_fire_dataset import ROOT, digest, iou, verified, write


def fingerprint(manifest):
    entries=[(r['id'],r['image_sha256']) for r in manifest['records']]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def merge_candidates(drafts,predictions,width,height):
    boxes=[];metadata=[];collapsed=0
    candidates=[]
    for p in predictions:
        if p['name'].lower() not in ['fire','smoke']:continue
        x,y,X,Y=p['xyxy'];x=max(0,min(width,x));X=max(0,min(width,X));y=max(0,min(height,y));Y=max(0,min(height,Y))
        if X<=x or Y<=y:continue
        candidates.append(([1 if p['name'].lower()=='fire' else 0,(x+X)/2/width,(y+Y)/2/height,(X-x)/width,(Y-y)/height], 'YOLO',p['confidence']))
    candidates.extend((b,'partial_draft',None) for b in drafts)
    def xyxy(b):return [b[1]-b[3]/2,b[2]-b[4]/2,b[1]+b[3]/2,b[2]+b[4]/2]
    for box,source,confidence in candidates:
        duplicate=next((i for i,b in enumerate(boxes) if b[0]==box[0] and iou(xyxy(b),xyxy(box))>=.85),None)
        if duplicate is not None:
            collapsed+=1
            if source not in metadata[duplicate]['sources']:metadata[duplicate]['sources'].append(source)
            continue
        boxes.append(box);metadata.append(dict(sources=[source],confidence=confidence))
    overlaps=[]
    for i,a in enumerate(boxes):
        for j,b in enumerate(boxes[i+1:],i+1):
            if a[0]!=b[0]:continue
            A=xyxy(a);B=xyxy(b);intersection=max(0,min(A[2],B[2])-max(A[0],B[0]))*max(0,min(A[3],B[3])-max(A[1],B[1]))
            containment=intersection/max(1e-12,min(a[3]*a[4],b[3]*b[4]))
            if iou(A,B)>=.3 or containment>=.8:overlaps.append([i,j])
    return boxes,metadata,overlaps,collapsed


def predict_candidates(out):
    from ultralytics import YOLO
    manifest=json.loads((out/'manifest.json').read_text());weight=ROOT/'models/fire_smoke_best.pt'
    before=digest(weight);model=YOLO(str(weight));rows=[]
    for r in manifest['records']:
        if digest(out/r['image'])!=r['image_sha256']:raise ValueError('Image changed')
        result=model.predict(str(out/r['image']),device='cpu',imgsz=640,conf=.30,iou=.70,rect=True,agnostic_nms=False,max_det=300,verbose=False)[0]
        rows.append(dict(id=r['id'],image_sha256=r['image_sha256'],boxes=[dict(name=result.names[int(b.cls[0])],confidence=float(b.conf[0]),xyxy=b.xyxy[0].tolist()) for b in result.boxes]))
    if digest(weight)!=before:raise ValueError('Weight changed')
    target=out/'automatic_predictions.json'
    if target.exists():shutil.copy2(target,out/('automatic_predictions_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.backup.json'))
    write(target,dict(weight_sha256=before,settings=dict(conf=.30,iou=.70,imgsz=640,device='cpu'),status='unreviewed_model_predictions',rows=rows))


def build(out,refresh=False):
    manifest=json.loads((out/'manifest.json').read_text())
    prediction_path=out/'automatic_predictions.json'
    predictions=json.loads((prediction_path if prediction_path.exists() else out/'baseline_predictions.json').read_text())
    if predictions['weight_sha256']!=digest(ROOT/'models/fire_smoke_best.pt'):raise ValueError('Prediction checkpoint mismatch')
    lookup={r['id']:r for r in predictions['rows']};rows=[]
    dest=out/'human_review'
    if dest.exists():
        if not refresh:raise ValueError('Review editor exists; use --refresh to back up and regenerate')
        shutil.copy2(dest/'index.html',dest/('index_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.backup.html'))
    dest.mkdir(exist_ok=True)
    ordered=sorted(manifest['records'],key=lambda r:(0 if r['period']=='day' and r['expected_fire'] else 1))
    for r in ordered:
        if digest(out/r['image'])!=r['image_sha256'] or lookup[r['id']]['image_sha256']!=r['image_sha256']:
            raise ValueError('Image/prediction identity mismatch')
        seed=[]
        for line in (out/r['draft_label']).read_text().splitlines():
            c,*box=map(float,line.split());seed.append([int(c),*box])
        merged,metadata,overlaps,collapsed=merge_candidates(seed,lookup[r['id']]['boxes'],r['width'],r['height'])
        rows.append(dict(id=r['id'],image_sha256=r['image_sha256'],period=r['period'],split=r['split'],seconds=r['seconds'],
            priority_day_fire=r['period']=='day' and r['expected_fire'],
            image_url=Path(os.path.relpath(out/r['image'],dest)).as_posix(),seed_boxes=merged,seed_metadata=metadata,
            draft_count=len(seed),overlap_pairs=overlaps,collapsed_duplicates=collapsed,predictions=lookup[r['id']]['boxes']))
    data=dict(dataset_fingerprint=fingerprint(manifest),records=rows)
    template=Path(__file__).with_name('fire_review.html').read_text()
    (dest/'index.html').write_text(template.replace('__REVIEW_DATA__',json.dumps(data,ensure_ascii=False).replace('</','<\\/')))
    write(out/'semiauto_inventory.json',dict(frames=len(rows),day_fire_first=sum(r['priority_day_fire'] for r in rows[:10]),
        candidates=sum(len(r['seed_boxes']) for r in rows),empty_frames=sum(not r['seed_boxes'] for r in rows),
        collapsed_duplicates=sum(r['collapsed_duplicates'] for r in rows),overlap_pairs=sum(len(r['overlap_pairs']) for r in rows),status='all_seed_candidates_unreviewed'))


def label_text(boxes):
    lines=[]
    if not isinstance(boxes,list):raise ValueError('Expected boxes list')
    for box in boxes:
        if not isinstance(box,list) or len(box)!=5:raise ValueError('Expected [class,cx,cy,w,h]')
        cls,x,y,w,h=box
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in box):
            raise ValueError('Nonfinite or nonnumeric label')
        if cls not in (0,1) or not (0<w<=1 and 0<h<=1 and w/2-1e-8<=x<=1-w/2+1e-8 and h/2-1e-8<=y<=1-h/2+1e-8):
            raise ValueError('Invalid normalized label bounds')
        lines.append(f'{int(cls)} {x:.10f} {y:.10f} {w:.10f} {h:.10f}')
    return '\n'.join(lines)+('\n' if lines else '')


def import_review(out,path):
    manifest=json.loads((out/'manifest.json').read_text());payload=json.loads(path.read_text())
    if payload.get('schema_version')!=1 or payload.get('dataset_fingerprint')!=fingerprint(manifest):raise ValueError('Review dataset mismatch')
    originals={r['id']:r for r in manifest['records']};seen=set();completed=[];revocations=[]
    for review in payload['records']:
        key=review['id']
        if key in seen or key not in originals:raise ValueError('Unknown or duplicate frame ID')
        seen.add(key);row=originals[key]
        if review['image_sha256']!=row['image_sha256']:raise ValueError('Review frame mismatch')
        if review['status'] not in ['pending','completed']:raise ValueError('Unknown review status')
        if review['status']=='pending':
            if row['review']['status']=='verified':revocations.append(key)
            continue
        if not review.get('reviewer','').strip() or set(review.get('complete_for_classes',[]))!={'smoke','fire'} or not review.get('completed_at'):
            raise ValueError('Missing explicit user verification')
        if not isinstance(review.get('fire_present'),bool) or any(b[0]==1 for b in review['boxes'])!=review['fire_present']:
            raise ValueError('Fire presence/labels mismatch')
        text=label_text(review['boxes']);completed.append((key,text,review))
    if not completed and not revocations:raise ValueError('No explicitly completed user reviews; nothing imported')
    proposed=copy.deepcopy(manifest);updates={key:(text,review) for key,text,review in completed}
    # Validate the entire proposed state in temporary storage before changing real labels.
    with tempfile.TemporaryDirectory() as temp:
        temp=Path(temp)
        for row in proposed['records']:
            if row['id'] in revocations:
                row['review']['status']='pending'
            (temp/row['image']).parent.mkdir(parents=True,exist_ok=True)
            (temp/row['image']).symlink_to(out/row['image'])
            if row['id'] in updates:
                text,review=updates[row['id']]
                row['review']=dict(status='verified',reviewer=review['reviewer'].strip(),complete_for_classes=['smoke','fire'],
                    fire_present=review['fire_present'],image_sha256=row['image_sha256'],
                    label_sha256=hashlib.sha256(text.encode()).hexdigest(),notes=review.get('notes',''),
                    completed_at=review['completed_at'],imported_from='explicit human-review JSON')
                label=temp/row['label'];label.parent.mkdir(parents=True,exist_ok=True);label.write_text(text)
            elif row['review']['status']=='verified':
                label=temp/row['label'];label.parent.mkdir(parents=True,exist_ok=True);label.symlink_to(out/row['label'])
        verified(temp,proposed)
    backup=out/'review_import_backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');backup.mkdir(parents=True)
    shutil.copy2(out/'manifest.json',backup/'manifest.json')
    shutil.copy2(path,backup/'submitted_review.json')
    for key,text,review in completed:
        label=out/originals[key]['label']
        if label.exists():shutil.copy2(label,backup/(key+'.txt'))
        label.parent.mkdir(parents=True,exist_ok=True);label.write_text(text)
    write(out/'manifest.json',proposed)


def evaluate(out):
    manifest=json.loads((out/'manifest.json').read_text());approved=verified(out,manifest)
    raw=json.loads((out/'baseline_predictions.json').read_text());predictions={r['id']:r for r in raw['rows']}
    metrics=[]
    for name,cls,thresholds in [('fire',1,[.30,.35]),('smoke',0,[.30,.50,.62])]:
        for split in ['val','test']:
            for period in ['day','sunset','night']:
                for threshold in thresholds:
                    frames=tp=fp=fn=negatives=false_alarm_frames=positives=missed_frames=0
                    for row,labels in approved:
                        if (row['split'],row['period'])!=(split,period):continue
                        prediction=predictions[row['id']]
                        if prediction['image_sha256']!=row['image_sha256']:raise ValueError('Stale prediction/image')
                        gt=[b for c,b in labels if c==cls];used=set();frames+=1
                        boxes=sorted([b for b in prediction['boxes'] if b['name'].lower()==name and b['confidence']>=threshold],key=lambda b:-b['confidence'])
                        for box in boxes:
                            score,j=max([(iou(box['xyxy'],b),j) for j,b in enumerate(gt) if j not in used],default=(0,-1))
                            if score>=.5:tp+=1;used.add(j)
                            else:fp+=1
                        fn+=len(gt)-len(used)
                        if gt:positives+=1;missed_frames+=not used
                        else:negatives+=1;false_alarm_frames+=bool(boxes)
                    metrics.append(dict(class_name=name,split=split,period=period,confidence=threshold,
                        frames=frames,tp=tp,fp=fp,fn=fn,instance_miss_rate=fn/(tp+fn) if tp+fn else None,
                        precision=tp/(tp+fp) if tp+fp else None,positive_frames=positives,
                        positive_frame_miss_rate=missed_frames/positives if positives else None,
                        negative_frames=negatives,false_alarm_frames=false_alarm_frames,
                        negative_frame_false_alarm_rate=false_alarm_frames/negatives if negatives else None))
    write(out/'human_review_evaluation.json',dict(status='partial_verified_subset_only' if approved else 'incomplete_no_user_verified_labels',
        user_verified_frames=len(approved),excluded_frames=len(manifest['records'])-len(approved),
        weight_sha256=raw['weight_sha256'],settings=raw['settings'],metrics=metrics,
        scope='Raw checkpoint boxes, confidence-ordered one-to-one IoU>=0.5. No UI sky/cloud filter, temporal alarm, mAP, or event evaluation. No claim of representative accuracy.'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['predict','build','import','evaluate']);parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--review-json',type=Path);args=parser.parse_args();out=args.dataset.resolve()
    if args.action=='predict':predict_candidates(out)
    elif args.action=='build':build(out,args.refresh)
    elif args.action=='import':
        if not args.review_json:parser.error('--review-json is required')
        import_review(out,args.review_json.resolve())
    else:evaluate(out)
    print(args.action,'finished:',out)


if __name__=='__main__':main()
