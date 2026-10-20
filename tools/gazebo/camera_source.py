"""Gazebo Transport worker and bounded BGR pipe reader (no Qt/YOLO imports).

The worker runs under Gazebo's Python; the reader runs under the tracker Python.
Only the newest callback frame is retained while stdout is busy.
"""
import argparse
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
import threading
import time

HEADER = struct.Struct('!III')  # width, height, packed BGR byte count
MAX_BYTES = 64 * 1024 * 1024


def to_bgr(msg):
    """Validate actual Image metadata, strip row padding, return packed BGR8."""
    enum = msg.DESCRIPTOR.fields_by_name['pixel_format_type'].enum_type
    value = enum.values_by_number.get(msg.pixel_format_type)
    name = value.name if value else 'UNKNOWN'
    channels = {'RGB_INT8': 3, 'BGR_INT8': 3, 'RGBA_INT8': 4,
                'BGRA_INT8': 4, 'L_INT8': 1}.get(name)
    w, h, step = msg.width, msg.height, msg.step
    if channels is None:
        raise ValueError(f'Unsupported pixel format: {name} ({msg.pixel_format_type})')
    if not w or not h or step < w * channels or step * h > MAX_BYTES or len(msg.data) != step * h:
        raise ValueError(f'Invalid image: {w}x{h}, step={step}, bytes={len(msg.data)}')
    packed = b''.join(msg.data[y*step:y*step+w*channels] for y in range(h))
    if w * h * 3 > MAX_BYTES:
        raise ValueError('BGR image exceeds size limit')
    if name == 'BGR_INT8':
        return packed
    out = bytearray(w * h * 3)
    if channels == 1:
        out[0::3] = out[1::3] = out[2::3] = packed
    else:
        rgb = name.startswith('RGB')
        out[0::3] = packed[2 if rgb else 0::channels]
        out[1::3] = packed[1::channels]
        out[2::3] = packed[0 if rgb else 2::channels]
    return bytes(out)


def worker(topic):
    from gz.transport13 import Node
    from gz.msgs10.image_pb2 import Image
    condition = threading.Condition()
    latest = [None]
    metadata = [None]
    last_error = [0.0]

    def receive(msg):
        try:
            pixels = to_bgr(msg)
            info = (msg.pixel_format_type, msg.width, msg.height, msg.step, len(msg.data))
            if info != metadata[0]:
                name = Image.DESCRIPTOR.fields_by_name['pixel_format_type'].enum_type.values_by_number[info[0]].name
                print(f'[Gazebo] {name} ({info[0]}) {info[1]}x{info[2]} step={info[3]} bytes={info[4]} -> BGR8', file=sys.stderr, flush=True)
                metadata[0] = info
            with condition:
                latest[0] = (msg.width, msg.height, pixels)
                condition.notify()
        except Exception as error:
            if time.monotonic() - last_error[0] >= 3:
                print(f'[Gazebo] {error}', file=sys.stderr, flush=True)
                last_error[0] = time.monotonic()

    node = Node()
    if not node.subscribe(Image, topic, receive):
        raise RuntimeError(f'Subscribe failed: {topic}')
    try:
        while True:
            with condition:
                condition.wait_for(lambda: latest[0] is not None)
                w, h, pixels = latest[0]
                latest[0] = None
            sys.stdout.buffer.write(HEADER.pack(w, h, len(pixels)))
            sys.stdout.buffer.write(pixels)
            sys.stdout.buffer.flush()
    finally:
        node.unsubscribe(topic)


def frames(topic, stop_event, process_changed=lambda p: None, timeout=5):
    """Yield BGR ndarrays; stop/timeout tears down the worker in bounded time."""
    import numpy as np
    default = Path.home() / 'Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python'
    python = os.environ.get('GAZEBO_PYTHON', str(default))
    process = subprocess.Popen([python, str(Path(__file__).resolve()), '--worker', '--topic', topic],
                               stdout=subprocess.PIPE, bufsize=0)
    process_changed(process)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    data = bytearray()
    expected = HEADER.size
    dimensions = None
    deadline = time.monotonic() + timeout
    try:
        while not stop_event.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError(f'No Gazebo frame for {timeout}s: {topic}')
            if not selector.select(0.1):
                continue
            chunk = os.read(process.stdout.fileno(), min(65536, expected - len(data)))
            if not chunk:
                raise RuntimeError(f'Gazebo worker disconnected (exit={process.poll()})')
            data.extend(chunk)
            if len(data) != expected:
                continue
            if dimensions is None:
                w, h, size = HEADER.unpack(data)
                if not w or not h or size != w*h*3 or size > MAX_BYTES:
                    raise ValueError('Invalid BGR pipe header')
                dimensions, expected = (h, w, 3), size
            else:
                frame = np.frombuffer(data, dtype=np.uint8).reshape(dimensions).copy()
                dimensions, expected = None, HEADER.size
                deadline = time.monotonic() + timeout
                yield frame
            data = bytearray()
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        process_changed(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--topic', default='/safety/probe/image')
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--output', type=Path, default=Path('diagnostics/gazebo/bgr_frame.ppm'))
    args = parser.parse_args()
    if args.worker:
        worker(args.topic)
        return
    stop = threading.Event()
    stream = frames(args.topic, stop, timeout=args.timeout)
    times = []
    try:
        for frame in stream:
            times.append(time.monotonic())
            if len(times) == 1:
                h, w = frame.shape[:2]
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(f'P6\n{w} {h}\n255\n'.encode() + frame[:, :, ::-1].tobytes())
                print(f'BGR frame: shape={frame.shape}, dtype={frame.dtype}, bytes={frame.nbytes}', flush=True)
            if len(times) >= args.count:
                break
        if len(times) < args.count:
            raise RuntimeError('Insufficient frames')
        print(f'PASS: {len(times)} BGR frames, {(len(times)-1)/max(times[-1]-times[0], 1e-9):.1f} FPS')
    finally:
        stop.set()
        stream.close()


if __name__ == '__main__':
    main()
