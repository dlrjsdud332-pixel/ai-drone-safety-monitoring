"""Discover and verify X500 image delivery before optionally launching the tracker.

Run with the existing tracker Python (numpy; pymavlink only for --motion).
Gazebo CLI/worker use the PX4 Pixi environment. Never sends flight commands.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
from camera_source import frames

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent


def discover(gz, world, model, timeout):
    deadline = time.monotonic() + timeout
    prefix = f'/world/{world}/model/{model}/'
    while time.monotonic() < deadline:
        try:
            result = subprocess.run([str(gz), 'topic', '-l'], capture_output=True,
                                    text=True, timeout=min(4, max(0.1, deadline-time.monotonic())))
        except subprocess.TimeoutExpired:
            continue
        if result.returncode:
            raise RuntimeError(f'Gazebo topic discovery failed: {result.stderr.strip()}')
        candidates = [line.strip() for line in result.stdout.splitlines()
                      if line.strip().startswith(prefix) and line.strip().endswith('/image')
                      and '/link/camera_link/sensor/camera/' in line]
        if len(candidates) > 1:
            raise RuntimeError(f'Ambiguous X500 image topics: {candidates}')
        if candidates:
            topic = candidates[0]
            info = subprocess.run([str(gz), 'topic', '-i', '-t', topic], capture_output=True,
                                  text=True, timeout=4, check=True)
            if 'gz.msgs.Image' not in info.stdout:
                raise RuntimeError(f'Not an Image publisher: {info.stdout}')
            print(info.stdout, flush=True)
            return topic
        time.sleep(0.2)
    raise TimeoutError(f'X500 Image topic not discovered for {world}/{model}')


def capture(topic, count, timeout, directory):
    stop = threading.Event()
    stream = frames(topic, stop, timeout=timeout)
    times = []
    shape = None
    try:
        for frame in stream:
            now = time.monotonic()
            h, w = frame.shape[:2]
            if (h, w) != (480, 640) or frame.dtype != np.uint8:
                raise RuntimeError(f'Unexpected X500 frame: {frame.shape} {frame.dtype}')
            times.append(now)
            shape = list(frame.shape)
            if len(times) == 1:
                (directory/'frame.ppm').write_bytes(f'P6\n{w} {h}\n255\n'.encode() + frame[:, :, ::-1].tobytes())
            if len(times) >= count:
                break
    finally:
        stop.set()
        stream.close()
    if len(times) < count:
        raise RuntimeError(f'Only {len(times)}/{count} frames')
    return {'count': len(times), 'shape': shape, 'fps': (len(times)-1)/max(times[-1]-times[0], 1e-9)}


def observe_motion(topic, duration, port, directory):
    """Pair fresh PX4 positions and BGR frames while the user moves SITL in QGC."""
    from pymavlink import mavutil
    link = mavutil.mavlink_connection(f'udpin:127.0.0.1:{port}', source_system=250)
    stop = threading.Event()
    stream = frames(topic, stop)
    first_position = None
    current_position = None
    position_at = 0
    first_image = None
    max_distance = max_difference = 0.0
    heartbeat_at = 0
    autopilot_system = None
    started = time.monotonic()
    print(f'Move SITL using QGC within {duration:g}s. Read-only observation on UDP {port}.', flush=True)
    try:
        for frame in stream:
            now = time.monotonic()
            # Bound draining time even if telemetry is produced faster than consumed.
            for _ in range(500):
                msg = link.recv_match(blocking=False)
                if msg is None:
                    break
                if msg.get_type() == 'HEARTBEAT' and msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_PX4:
                    heartbeat_at = now
                    autopilot_system = msg.get_srcSystem()
                if (msg.get_type() == 'GLOBAL_POSITION_INT' and autopilot_system is not None
                        and msg.get_srcSystem() == autopilot_system):
                    current_position = np.array([msg.lat/1e7, msg.lon/1e7, msg.relative_alt/1000.0])
                    position_at = now
            if current_position is not None and now-position_at < 1 and now-heartbeat_at < 3:
                small = frame[::8, ::8].astype(np.float32)
                if first_position is None:
                    first_position = current_position.copy()
                    first_image = small.copy()
                    h, w = frame.shape[:2]
                    (directory/'motion-before.ppm').write_bytes(f'P6\n{w} {h}\n255\n'.encode()+frame[:, :, ::-1].tobytes())
                delta = current_position-first_position
                north = delta[0]*111320
                east = delta[1]*111320*np.cos(np.deg2rad(first_position[0]))
                distance = float(np.linalg.norm([north, east, delta[2]]))
                difference = float(np.mean(np.abs(small-first_image)))
                max_distance = max(max_distance, distance)
                max_difference = max(max_difference, difference)
                if distance >= 1 and difference >= 2:
                    h, w = frame.shape[:2]
                    (directory/'motion-after.ppm').write_bytes(f'P6\n{w} {h}\n255\n'.encode()+frame[:, :, ::-1].tobytes())
                    return {'observed': True, 'displacement_m': distance, 'bgr_mean_difference': difference,
                            'note': 'Concurrent fresh PX4 position and image change; inspect before/after images.'}
            if now-started >= duration:
                raise RuntimeError(f'Motion NOT verified: max displacement={max_distance:.2f}m, image difference={max_difference:.2f}')
    finally:
        stop.set()
        stream.close()
        link.close()
    raise RuntimeError('Motion stream ended')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--world', default='x500_camera')
    parser.add_argument('--model', default='x500_mono_cam_0')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--motion', type=float, default=0, metavar='SECONDS')
    parser.add_argument('--telemetry-port', type=int, default=14540)
    parser.add_argument('--tracker-python', type=Path, help='Launch tracker with this Python only AFTER checks pass')
    parser.add_argument('--output', type=Path, default=PROJECT/'diagnostics/gazebo/x500')
    args = parser.parse_args()
    if args.count < 2 or args.timeout <= 0 or args.motion < 0:
        parser.error('count >= 2, timeout > 0, motion >= 0 required')
    px4 = Path(os.environ.get('PX4_SOURCE_DIR', str(Path.home()/'Desktop/PX4-Autopilot')))
    prefix = px4/'Tools/setup/macos/.pixi/envs/default'
    os.environ.setdefault('GZ_IP', '127.0.0.1')
    os.environ.setdefault('GZ_PARTITION', 'safety_x500_camera')
    os.environ.setdefault('GAZEBO_PYTHON', str(prefix/'bin/python'))
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'verified': False, 'partition': os.environ['GZ_PARTITION'],
              'world': args.world, 'model': args.model, 'qgc_ui_verified': False}
    try:
        topic = discover(prefix/'bin/gz', args.world, args.model, args.timeout)
        report['topic'] = topic
        report['frames'] = capture(topic, args.count, args.timeout, args.output)
        if args.motion:
            report['motion'] = observe_motion(topic, args.motion, args.telemetry_port, args.output)
        report['verified'] = True
    except Exception as error:
        report['error'] = str(error)
    (args.output/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2), flush=True)
    if not report['verified']:
        return 1
    if args.tracker_python:
        # This subprocess starts only after live topic/type and BGR frame checks pass.
        return subprocess.call([str(args.tracker_python.expanduser().resolve()),
                                str(PROJECT/'legacy/drone_tracker.py'),
                                'gazebo://' + topic, '--flight-adapter', 'PX4',
                                '--px4-telemetry-port', str(args.telemetry_port)], cwd=PROJECT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
