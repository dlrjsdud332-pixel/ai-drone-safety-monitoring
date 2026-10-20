"""Measure partial visual targets; never compute full-frame accuracy/false positives."""
import argparse
import json
from pathlib import Path
from evaluate_crowd_detection import iou


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--targets',type=Path,required=True);a=p.parse_args()
    import cv2
    targets=json.loads(a.targets.read_text());rows=[];totals={m:0 for m in ('baseline832','high1280','tiles832')}
    for f in targets['frames']:
        folder=a.run/Path(f['video']).stem;native=f['native_frame'];panels=[]
        for method in totals:
            result=json.loads((folder/f'{method}.json').read_text());row=next(r for r in result['rows'] if r['native_frame']==native)
            im=cv2.imread(str(folder/f'{Path(f["video"]).stem}_{native}.png'))
            matches=[];used=set()
            for n,b in enumerate(f['boxes']):
                score,index=max(((iou(b,p['xyxy']),j) for j,p in enumerate(row['detections']) if j not in used),default=(0,-1))
                hit=score>=.5
                if hit:used.add(index);totals[method]+=1
                matches.append(dict(target=n,hit=hit,iou=score,height=b[3]-b[1],width=b[2]-b[0]))
                x,y,X,Y=b;cv2.rectangle(im,(x,y),(X,Y),(0,255,0) if hit else (0,0,255),2);cv2.putText(im,f'T{n}',(x,y-4),0,.6,(0,220,255),2)
            for b in row['detections']:
                x,y,X,Y=map(round,b['xyxy']);cv2.rectangle(im,(x,y),(X,Y),(255,180,0),1)
            cv2.putText(im,method+' | PARTIAL VISUAL TARGETS',(15,30),0,.8,(0,0,255),2)
            panels.append(cv2.resize(im,(960,540)));rows.append(dict(video=f['video'],method=method,targets=matches))
        cv2.imwrite(str(folder/f'{native}_target_comparison.jpg'),cv2.vconcat(panels))
    report=dict(status='Assistant visual partial targets; not human-confirmed exhaustive ground truth',targets=sum(len(f['boxes']) for f in targets['frames']),hits=totals,rows=rows,
        precision=None,false_positives=None,note='Only partial target coverage at IoU .5; no full recall, accuracy, crowd count, or false positive claims.')
    (a.run/'partial_target_results.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='rows'},indent=2))


if __name__=='__main__':main()
