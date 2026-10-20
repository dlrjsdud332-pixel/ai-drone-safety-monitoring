"""Evaluate comparison outputs ONLY on human-confirmed complete annotations."""
import argparse
import json
from pathlib import Path


def iou(a,b):
    area=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-area
    return area/union if union>0 else 0


def match(pred,truth,threshold=.5):
    used=set();tp=0
    for p in sorted(pred,key=lambda b:b['confidence'],reverse=True):
        candidates=[(iou(p['xyxy'],b),i) for i,b in enumerate(truth) if i not in used]
        score,index=max(candidates,default=(0,-1))
        if score>=threshold:used.add(index);tp+=1
    return dict(tp=tp,fp=len(pred)-tp,fn=len(truth)-tp)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--labels',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();review=json.loads(a.labels.read_text());totals={};used=[]
    for f in review['frames']:
        if not(f.get('status')=='reviewed' and f.get('confirmed_by')=='human' and f.get('complete') is True):continue
        if f.get('ignore_regions'):raise ValueError('Ignore-region evaluation not implemented; use exhaustive frames without ignored regions')
        truth=f['people']
        for b in truth:
            if len(b)!=4 or not(0<=b[0]<b[2] and 0<=b[1]<b[3]):raise ValueError('Invalid xyxy annotation')
        folder=a.run/Path(f['video']).stem
        for method in ('baseline832','high1280','tiles832'):
            data=json.loads((folder/f'{method}.json').read_text())
            rows=[r for r in data['rows'] if r['native_frame']==f['native_frame']]
            if len(rows)!=1:raise ValueError('Annotation native frame not unique in prediction output')
            for kind in ('detections','tracks'):
                key=f'{method}/{kind}';t=totals.setdefault(key,dict(tp=0,fp=0,fn=0,frames=0,visible_people=0))
                result=match(rows[0][kind],truth)
                for k,v in result.items():t[k]+=v
                t['frames']+=1;t['visible_people']+=len(truth)
        used.append(f['key'])
    for t in totals.values():
        t['precision']=t['tp']/(t['tp']+t['fp']) if t['tp']+t['fp'] else None
        t['recall']=t['tp']/(t['tp']+t['fn']) if t['tp']+t['fn'] else None
    report=dict(human_confirmed_frames=len(used),frames_used=used,metrics=totals,status='evaluated' if used else 'INCOMPLETE: no human-confirmed exhaustive person labels',iou=.5,
        note='Raw detector and tracked-person metrics separate. Unique ID counts are not IDF1; temporal identity labels required.')
    a.output.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':main()
