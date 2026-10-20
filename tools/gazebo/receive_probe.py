"""Verify actual RGB payloads using Pixi Python, without OpenCV or the UI."""
import argparse
from pathlib import Path
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', default='/safety/probe/image')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--output', type=Path, default=Path('diagnostics/gazebo/frame.ppm'))
    args = parser.parse_args()
    from gz.transport13 import Node
    from gz.msgs10.image_pb2 import Image
    done = threading.Event()
    frames = []

    def receive(msg):
        # gz.msgs.PixelFormatType: RGB_INT8 = 3. Reject other formats explicitly.
        if msg.pixel_format_type != 3:
            print(f'Unsupported pixel format: {msg.pixel_format_type}', flush=True)
            return
        width, height, stride = msg.width, msg.height, msg.step
        if not width or not height or stride < width * 3 or len(msg.data) < stride * height:
            print('Invalid image dimensions/stride/payload', flush=True)
            return
        if not frames:
            pixels = b''.join(msg.data[y * stride:y * stride + width * 3] for y in range(height))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(f'P6\n{width} {height}\n255\n'.encode() + pixels)
            print(f'RGB frame {width}x{height}, {len(pixels)} bytes -> {args.output}', flush=True)
        frames.append(time.monotonic())
        if len(frames) >= 10:
            done.set()

    node = Node()
    if not node.subscribe(Image, args.topic, receive):
        raise SystemExit(f'Subscribe failed: {args.topic}')
    try:
        if not done.wait(args.timeout):
            raise SystemExit(f'FAIL: received {len(frames)}/10 valid RGB frames in {args.timeout}s')
        print(f'PASS: {len(frames)} valid frames, {(len(frames)-1)/(frames[-1]-frames[0]):.1f} FPS')
    finally:
        node.unsubscribe(args.topic)


if __name__ == '__main__':
    main()
