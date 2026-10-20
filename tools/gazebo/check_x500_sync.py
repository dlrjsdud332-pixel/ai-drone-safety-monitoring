"""Read-only X500 preflight: reject duplicate publishers and invalid sensor time.

Runs the Transport probe in a bounded child process. No parameters or sensors
are changed; existing servers are never stopped by this tool.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time


def topics(world, model):
    base = f'/world/{world}/model/{model}/link/'
    return {'clock': f'/world/{world}/clock',
            'imu': base+'base_link/sensor/imu_sensor/imu',
            'barometer': base+'base_link/sensor/air_pressure_sensor/air_pressure',
            'camera': base+'camera_link/sensor/camera/image'}


def assess(role, publishers, samples):
    errors=[]
    if role=='gazebo':
        for name, count in publishers.items():
            if count:
                errors.append(f'{name}: {count} existing publisher(s); stop the previous world before launching another')
    else:
        for name,count in publishers.items():
            if count!=1:
                errors.append(f'{name}: expected exactly 1 publisher, got {count}')
        for name in ['clock','imu','barometer']:
            values=samples[name]
            if len(values)<3:
                errors.append(f'{name}: insufficient timestamp samples ({len(values)})')
                continue
            diffs=[b-a for a,b in zip(values,values[1:])]
            if min(diffs)<0:
                errors.append(f'{name}: timestamp moved backwards')
            if name!='clock' and min(diffs)<=0:
                errors.append(f'{name}: duplicate/non-increasing sensor timestamp')
            if values[-1]<=values[0]:
                errors.append(f'{name}: simulation time did not advance')
            if name=='barometer' and max(diffs)>300_000_000:
                errors.append('barometer: simulated timestamp gap exceeds PX4 300ms timeout')
    return errors


def probe(args):
    # Fail closed if the environment cannot run discovery; empty results would
    # otherwise incorrectly look like an unused world in a restricted sandbox.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1',0))
    from gz.transport13 import Node
    from gz.msgs10.clock_pb2 import Clock
    from gz.msgs10.imu_pb2 import IMU
    from gz.msgs10.fluid_pressure_pb2 import FluidPressure
    node=Node()
    mapping=topics(args.world,args.model)
    samples={name:[] for name in ['clock','imu','barometer']}
    arrivals={name:[] for name in samples}
    lock=threading.Lock()
    callbacks=[]
    def callback(name):
        def receive(message):
            stamp=message.sim if name=='clock' else message.header.stamp
            with lock:
                samples[name].append(stamp.sec*1_000_000_000+stamp.nsec)
                arrivals[name].append(time.monotonic())
        return receive
    try:
        if args.role=='px4':
            for name,kind in [('clock',Clock),('imu',IMU),('barometer',FluidPressure)]:
                cb=callback(name)
                callbacks.append(cb)
                if not node.subscribe(kind,mapping[name],cb):
                    raise RuntimeError(f'Subscribe failed: {mapping[name]}')
        # Discovery and message sampling run concurrently in Transport threads.
        time.sleep(args.seconds)
        publishers={}
        details={}
        for name,topic in mapping.items():
            pubs,_=node.topic_info(topic)
            publishers[name]=len(pubs)
            details[name]=[{'type':pub.msg_type_name,'control':pub.ctrl} for pub in pubs]
        with lock:
            errors=assess(args.role,publishers,samples)
            metrics={}
            now=time.monotonic()
            for name,values in samples.items():
                diffs=[b-a for a,b in zip(values,values[1:])]
                arrival_gaps=[b-a for a,b in zip(arrivals[name],arrivals[name][1:])]
                metrics[name]={'samples':len(values),'backward_steps':sum(d<0 for d in diffs),
                               'duplicate_stamps':sum(d==0 for d in diffs),
                               'first_ns':values[0] if values else None,'last_ns':values[-1] if values else None,
                               'max_wall_gap_ms':max(arrival_gaps)*1000 if arrival_gaps else None}
                if args.role=='px4' and (not arrivals[name] or now-arrivals[name][-1]>.3):
                    errors.append(f'{name}: no recent sample in the last 300ms')
        return {'ok':not errors,'role':args.role,'partition':os.environ.get('GZ_PARTITION'),
                'world':args.world,'model':args.model,'topics':mapping,
                'publisher_counts':publishers,'publishers':details,'time_samples':metrics,'errors':errors}
    finally:
        if args.role=='px4':
            for name in samples:
                node.unsubscribe(mapping[name])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role',choices=['gazebo','px4'],required=True)
    parser.add_argument('--world',default='x500_camera')
    parser.add_argument('--model',default='x500_mono_cam_0')
    parser.add_argument('--seconds',type=float,default=3)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 1<=args.seconds<=30:
        parser.error('seconds must be between 1 and 30')
    if args.worker:
        try:
            report=probe(args)
        except Exception as error:
            report={'ok':False,'error':str(error),'partition':os.environ.get('GZ_PARTITION')}
    else:
        command=[sys.executable,str(Path(__file__).resolve()),'--worker','--role',args.role,
                 '--world',args.world,'--model',args.model,'--seconds',str(args.seconds)]
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=args.seconds+8)
            if not result.stdout.strip():
                raise RuntimeError(result.stderr.strip() or f'Probe exited {result.returncode}')
            report=json.loads(result.stdout)
            if result.returncode and report.get('ok'):
                report={'ok':False,'error':f'Probe exited {result.returncode}'}
        except (subprocess.SubprocessError,ValueError,RuntimeError) as error:
            report={'ok':False,'error':str(error),'partition':os.environ.get('GZ_PARTITION')}
    encoded=json.dumps(report,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded+'\n')
    print(encoded,flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
