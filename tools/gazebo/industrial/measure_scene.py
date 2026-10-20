"""Separate camera delivery benchmarking from offline YOLO detection checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from camera_source import frames
from verify_x500_camera import discover

PROJECT = HERE.parents[2]
OUTPUT = PROJECT/'diagnostics/gazebo/industrial/benchmark'


def write_json(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


def capture(args):
    prefix=Path(os.environ.get('PX4_SOURCE_DIR', str(Path.home()/'Desktop/PX4-Autopilot')))/'Tools/setup/macos/.pixi/envs/default'
    os.environ.setdefault('GZ_IP','127.0.0.1')
    os.environ.setdefault('GZ_PARTITION','safety_x500_camera')
    os.environ.setdefault('GAZEBO_PYTHON',str(prefix/'bin/python'))
    directory=OUTPUT/args.label
    directory.mkdir(parents=True,exist_ok=True)
    report={'verified':False,'label':args.label,'partition':os.environ['GZ_PARTITION'],
            'measurement':'BGR delivery FPS; excludes offline YOLO and GUI', 'samples':[]}
    write_json(directory/'capture.json',report)
    stop=threading.Event()
    stream=None
    try:
        topic=discover(prefix/'bin/gz','x500_camera','x500_mono_cam_0',args.timeout)
        report['topic']=topic
        stream=frames(topic,stop,timeout=args.timeout)
        times=[]
        last_saved=0
        for index,frame in enumerate(stream):
            if frame.shape!=(480,640,3) or frame.dtype!=np.uint8:
                raise ValueError(f'Unexpected image {frame.shape}/{frame.dtype}')
            if index<20:
                continue
            now=time.monotonic()
            times.append(now)
            if now-last_saved>=3 and len(report['samples'])<10:
                path=directory/f'frame_{len(report["samples"]):02d}.ppm'
                path.write_bytes(b'P6\n640 480\n255\n'+frame[:,:,::-1].tobytes())
                report['samples'].append(str(path.relative_to(PROJECT)))
                last_saved=now
            if len(times)>=args.count:
                break
        if len(times)<args.count:
            raise ValueError('Insufficient frames')
        gaps=np.diff(times)
        fps=(len(times)-1)/(times[-1]-times[0])
        report.update(verified=True,frames=len(times),shape=[480,640,3],warmup_frames=20,
                      delivery_fps=fps,within_9_to_11_fps=bool(9<=fps<=11),
                      p95_interarrival_ms=float(np.quantile(gaps,.95)*1000),
                      max_interarrival_ms=float(gaps.max()*1000),
                      real_time_factor=None)
        try:
            stats=subprocess.run([str(prefix/'bin/gz'),'topic','-e','-n','1','--json-output','-t','/world/x500_camera/stats'],capture_output=True,text=True,timeout=4,check=True)
            raw=json.loads(stats.stdout)
            if not isinstance(raw,dict):
                raise ValueError('Expected one WorldStatistics JSON object')
            report['real_time_factor']=raw.get('real_time_factor',raw.get('realTimeFactor'))
        except (ValueError,subprocess.SubprocessError) as error:
            report['stats_note']=f'RTF unavailable: {error}'
    except Exception as error:
        report['error']=str(error)
    finally:
        stop.set()
        if stream is not None:
            stream.close()
    write_json(directory/'capture.json',report)
    return 0 if report['verified'] else 1


def compare(args):
    reports=[]
    for label in ['baseline','worker','site','effects']:
        path=OUTPUT/label/'capture.json'
        if path.is_file():
            report=json.loads(path.read_text())
            if report.get('verified'):
                reports.append(report)
    baseline=next((r for r in reports if r['label']=='baseline'),None)
    if baseline is None:
        raise ValueError('A successful baseline capture is required')
    result={'baseline_fps':baseline['delivery_fps'],'stages':[]}
    for report in reports:
        result['stages'].append({k:report.get(k) for k in ['label','delivery_fps','within_9_to_11_fps','p95_interarrival_ms','real_time_factor']})
        result['stages'][-1]['fps_change_percent']=100*(report['delivery_fps']/baseline['delivery_fps']-1)
    result['note']='A capped 10Hz sensor can mask extra GPU load. Compare RTF and interarrival gaps at the same pose/UI load.'
    write_json(OUTPUT/'comparison.json',result)
    return 0


def detect(args):
    path=OUTPUT/args.label/'capture.json'
    capture_report=json.loads(path.read_text())
    if not capture_report.get('verified') or not capture_report.get('samples'):
        raise ValueError('Detection requires an actual successful capture')
    settings=[('person','yolo11s.pt',.35,832,[0]),
              ('fire_smoke','fire_smoke_best.pt',.30,640,None),
              ('ppe','ppe_best.pt',.40,640,[0,1,2,4])]
    results={'label':args.label,'inference_completed':False,'checks':[],
             'note':'Per-frame model predictions, not temporal tracker alerts, ground-truth accuracy, or a test on real human/fire footage.'}
    out=OUTPUT/args.label/'detections'
    out.mkdir(exist_ok=True)
    write_json(out/'results.json',results)
    from ultralytics import YOLO
    for kind,filename,conf,size,classes in settings:
        weights=PROJECT/'models'/filename
        if not weights.is_file():
            raise ValueError(f'Existing model missing: {weights}')
        model=YOLO(str(weights))
        for sample in capture_report['samples']:
            image=PROJECT/sample
            result=model.predict(str(image),device=args.device,conf=conf,imgsz=size,classes=classes,verbose=False)[0]
            rows=[]
            for box in result.boxes:
                class_id=int(box.cls[0])
                rows.append({'class_id':class_id,'class_name':result.names[class_id],
                             'confidence':float(box.conf[0]),'xyxy':box.xyxy[0].tolist()})
            result.save(filename=str(out/f'{kind}_{image.stem}.jpg'))
            results['checks'].append({'model':kind,'sample':sample,'detections':rows,'speed_ms':result.speed})
    results['inference_completed']=True
    results['summary']={kind: {'any_detection':any(c['detections'] for c in results['checks'] if c['model']==kind),
                            'frames_with_detections':sum(bool(c['detections']) for c in results['checks'] if c['model']==kind),
                            'frames_checked':sum(c['model']==kind for c in results['checks'])}
                        for kind,_,_,_,_ in settings}
    write_json(out/'results.json',results)
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    cap=sub.add_parser('capture')
    cap.add_argument('label',choices=['baseline','worker','site','effects'])
    cap.add_argument('--count',type=int,default=300)
    cap.add_argument('--timeout',type=float,default=10)
    sub.add_parser('compare')
    det=sub.add_parser('detect')
    det.add_argument('label',choices=['baseline','worker','site','effects'])
    det.add_argument('--device',default='cpu')
    args=parser.parse_args()
    if args.command=='capture' and (args.count<2 or args.timeout<=0):
        parser.error('count >=2 and timeout >0 required')
    try:
        return {'capture':capture,'compare':compare,'detect':detect}[args.command](args)
    except Exception as error:
        print(f'NOT VERIFIED: {error}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
