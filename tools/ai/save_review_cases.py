"""Save explicitly reviewed examples; never infer FP/FN from count changes alone."""
import json
from pathlib import Path
import shutil
from compare_detection import overlap

ROOT=Path(__file__).resolve().parents[2]


def main():
    import cv2
    out=ROOT/'diagnostics/ai_tuning/cases'
    out.mkdir(parents=True,exist_ok=True)
    report=json.loads((ROOT/'diagnostics/ai_tuning/comparison/results.json').read_text())
    specs=[
      {'id':'person_miss','class':'person','group':'person_vehicle','key':'7949326-hd_1920_1080_30fps_66',
       'region':[225,855,269,961],'verdict':'confirmed_visible_person_missed_by_all_tested_settings',
       'note':'Man in dark T-shirt in foreground cluster; approximate human-reviewed region, not exhaustive GT.'},
      {'id':'vehicle_resolution_regression','class':'car','group':'person_vehicle','key':'7949326-hd_1920_1080_30fps_594',
       'region':[666,402,768,440],'verdict':'baseline_car_present_but_640_input_misses_this_car',
       'note':'White car on road; approximate human-reviewed region, not an exhaustive vehicle annotation.'},
      {'id':'visible_flame_miss','class':'fire','group':'fire_smoke','key':'4825783-hd_1920_1080_30fps_49',
       'region':[15,545,170,690],'verdict':'visible_flame_region_no_baseline_fire_detection',
       'note':'Flames visible at forest/field boundary; smoke detection does not mean fire recall is complete.'},
      {'id':'nested_fire','class':'fire','group':'fire_smoke','key':'14297188_1920_1080_24fps_299',
       'region':[850,515,1008,620],'verdict':'nested_same_fire_region_boxes; possible_duplicate_not_separate_large_fire_error',
       'note':'Same central connected flame region. Nested IoU ~0.19 and containment 1.0; raising conf to .40 removes both central boxes, so no verified remedy.'},
      {'id':'normal_large_smoke','class':'smoke','group':'fire_smoke','key':'4825783-hd_1920_1080_30fps_49',
       'region':[0,21,1010,683],'verdict':'legitimate_large_smoke_region_not_a_duplicate',
       'note':'Large smoke plume visibly fills this area. Do not shrink/remove solely because area >25%.'},
      {'id':'possible_light_false_positive','class':'fire','group':'fire_smoke','key':'14297188_1920_1080_24fps_299',
       'region':[1730,620,1770,655],'verdict':'unconfirmed_false_positive_candidate',
       'note':'Tiny distant light source; cannot reliably distinguish flame from artificial light in this frame. Not counted as a confirmed FP.'}]
    for case in specs:
        folder=out/case['id'];folder.mkdir(exist_ok=True)
        rows=[r for r in report['rows'] if r['key']==case['key'] and r['group']==case['group']]
        shutil.copy2(rows[0]['raw'],folder/'raw.png')
        image=cv2.imread(rows[0]['raw']);x1,y1,x2,y2=case['region'];pad=40
        cv2.imwrite(str(folder/'context.png'),image[max(0,y1-pad):min(image.shape[0],y2+pad),max(0,x1-pad):min(image.shape[1],x2+pad)])
        marked=image.copy();cv2.rectangle(marked,(x1,y1),(x2,y2),(0,255,255),2)
        cv2.imwrite(str(folder/'review_region.jpg'),marked)
        case['settings']=[]
        for row in rows:
            shutil.copy2(row['annotated'],folder/f'{row["setting"]}.jpg')
            candidates=[b for b in row['boxes'] if b['class_name']==case['class']]
            case['settings'].append({'name':row['setting'],'class_count':len(candidates),
                'max_iou_with_review_region':max([overlap(b['xyxy'],case['region'])[0] for b in candidates] or [0]),
                'boxes':candidates})
        (folder/'review.json').write_text(json.dumps(case,indent=2)+'\n')
    coverage={'person':{'FN':'person_miss','FP':'No confirmed case in this small reviewed subset'},
              'vehicle':{'FN':'vehicle_resolution_regression (candidate setting regression)','FP':'No confirmed case in this small reviewed subset'},
              'fire':{'FN':'visible_flame_miss','FP':'possible_light_false_positive remains unconfirmed'},
              'smoke':{'FN':'No confirmed miss in sampled positives','FP':'No confirmed case; smoke-only/negative inputs not supplied'}}
    (out/'coverage.json').write_text(json.dumps({'cases':specs,'coverage':coverage,
        'scope':'Review examples only; incomplete approximate annotations, not a precision/recall dataset.'},indent=2)+'\n')
    print('Saved',len(specs),'review cases; no unconfirmed FP promoted to ground truth.')


if __name__=='__main__':
    main()
