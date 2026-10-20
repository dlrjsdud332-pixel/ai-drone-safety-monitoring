"""Diagnostic crops: test scale/context effects, never change the camera FOV."""
import json
from pathlib import Path
from compare_detection import overlap

ROOT=Path(__file__).resolve().parents[2]


def main():
    import cv2
    from ultralytics import YOLO
    specs=[('person_miss','person',[100,730,600,1080],'yolo11s.pt',832,.35,[0,2,3,5,7]),
           ('visible_flame_miss','fire',[0,450,500,800],'fire_smoke_best.pt',640,.30,None)]
    rows=[]
    for case,label,region,weights,size,conf,classes in specs:
        folder=ROOT/'diagnostics/ai_tuning/cases'/case
        review=json.loads((folder/'review.json').read_text())
        image=cv2.imread(str(folder/'raw.png'));x1,y1,x2,y2=region;crop=image[y1:y2,x1:x2]
        result=YOLO(str(ROOT/'models'/weights)).predict(crop,device='cpu',imgsz=size,conf=conf,
                  iou=.5 if label=='person' else .7,classes=classes,verbose=False)[0]
        cv2.imwrite(str(folder/'diagnostic_crop.jpg'),result.plot(line_width=1))
        boxes=[]
        for b in result.boxes:
            xyxy=b.xyxy[0].tolist();mapped=[xyxy[0]+x1,xyxy[1]+y1,xyxy[2]+x1,xyxy[3]+y1]
            boxes.append({'class':result.names[int(b.cls[0])],'confidence':float(b.conf[0]),
                          'xyxy_in_original':mapped,'iou_with_review_region':overlap(mapped,review['region'])[0]})
        rows.append({'case':case,'crop_region':region,'boxes':boxes,
                     'note':'Crop changes object scale AND context. Diagnostic only, not an applied model setting or full-frame accuracy gain.'})
    out=ROOT/'diagnostics/ai_tuning/crop-diagnostic.json'
    out.write_text(json.dumps(rows,indent=2)+'\n')
    print(json.dumps(rows,indent=2))


if __name__=='__main__':
    main()
