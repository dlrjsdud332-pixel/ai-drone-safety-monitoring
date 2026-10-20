"""Offline conversion, pipe lifecycle and existing Channel integration tests."""
import ast
from collections import deque
import importlib.util
import os
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from gz.msgs10.image_pb2 import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
spec = importlib.util.spec_from_file_location('camera_source', HERE / 'camera_source.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


def image(name, data, width=2, height=1, step=8):
    enum = Image.DESCRIPTOR.fields_by_name['pixel_format_type'].enum_type
    return Image(width=width, height=height, step=step, data=data,
                 pixel_format_type=enum.values_by_name[name].number)


class CameraTests(unittest.TestCase):
    def test_rgb_padding_and_ownership(self):
        msg = image('RGB_INT8', bytes([255, 0, 0, 0, 255, 0, 99, 99]))
        self.assertEqual(source.to_bgr(msg), bytes([0, 0, 255, 0, 255, 0]))

    def test_supported_formats(self):
        cases = [('BGR_INT8', b'\x01\x02\x03', b'\x01\x02\x03'),
                 ('RGBA_INT8', b'\x01\x02\x03\xff', b'\x03\x02\x01'),
                 ('BGRA_INT8', b'\x01\x02\x03\xff', b'\x01\x02\x03'),
                 ('L_INT8', b'\x07', b'\x07\x07\x07')]
        for name, payload, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(source.to_bgr(image(name, payload, 1, 1, len(payload))), expected)

    def test_reject_malformed(self):
        for msg in [image('RGB_INT8', b'123', step=3),
                    image('RGB_INT8', b'123', step=8),
                    image('RGB_INT16', b'12345678'),
                    image('RGB_INT8', b'', width=0, step=0)]:
            with self.assertRaises(ValueError):
                source.to_bgr(msg)

    def fake_worker(self, directory, body):
        path = Path(directory) / 'worker'
        path.write_text('#!' + os.sys.executable + '\n' + body)
        path.chmod(0o755)
        return patch.dict(os.environ, {'GAZEBO_PYTHON': str(path)})

    def test_pipe_and_stop(self):
        with tempfile.TemporaryDirectory() as directory, self.fake_worker(directory,
                "import sys,struct,time\nsys.stdout.buffer.write(struct.pack('!III',2,1,6)+bytes([0,0,255,0,255,0]))\nsys.stdout.buffer.flush()\ntime.sleep(10)\n"):
            stop = threading.Event()
            processes = []
            stream = source.frames('/test', stop, processes.append, timeout=1)
            frame = next(stream)
            self.assertEqual(frame.shape, (1, 2, 3))
            self.assertEqual(frame[0, 0].tolist(), [0, 0, 255])
            stop.set()
            with self.assertRaises(StopIteration):
                next(stream)
            self.assertIsNone(processes[-1])
            self.assertIsNotNone(processes[0].poll())

    def test_timeout_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, self.fake_worker(directory, 'import time\ntime.sleep(10)\n'):
            processes = []
            stream = source.frames('/test', threading.Event(), processes.append, timeout=0.2)
            with self.assertRaises(TimeoutError):
                next(stream)
            self.assertIsNotNone(processes[0].poll())
            self.assertIsNone(processes[-1])

    def test_channel_existing_publish_path(self):
        # Load the real Channel class without importing Qt/Torch/YOLO.
        tree = ast.parse((HERE.parent.parent / 'legacy/drone_tracker.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Channel')
        namespace = dict(threading=threading, time=time, deque=deque,
                         EVENT_PRE_SECONDS=5, EVENT_CLIP_FPS=5)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), '<Channel>', 'exec'), namespace)
        channel = namespace['Channel'](1, '', False)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 2] = 255
        channel.source = 'gazebo:///safety/probe/image'
        with patch('tools.gazebo.camera_source.frames', return_value=(f for f in [frame])) as subscribe:
            channel.read_gazebo()
            subscribe.assert_called_once()
            self.assertEqual(subscribe.call_args.args[0], '/safety/probe/image')
        with channel.lock:
            self.assertIs(channel.pending, frame)
            self.assertEqual(channel.sequence, 1)
            self.assertEqual(channel.status, 'Playing')
            self.assertEqual(channel.event_buffer[-1][1][0, 0].tolist(), [0, 0, 255])
        channel.stop()
        self.assertTrue(channel.stop_event.is_set())


if __name__ == '__main__':
    unittest.main()
