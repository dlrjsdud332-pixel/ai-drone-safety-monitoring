"""Offline checks for topic identity and the live-frame-before-tracker gate."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import verify_x500_camera as verify

TOPIC = '/world/x500_camera/model/x500_mono_cam_0/link/camera_link/sensor/camera/image'


class VerificationTests(unittest.TestCase):
    def test_discovery_ignores_probe_and_other_models(self):
        topics = '/safety/probe/image\n/world/x500_camera/model/other/link/camera_link/sensor/camera/image\n'+TOPIC+'\n'
        with patch.object(verify.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, topics, ''),
                subprocess.CompletedProcess([], 0, 'Publishers: gz.msgs.Image', '')]):
            self.assertEqual(verify.discover('/gz', 'x500_camera', 'x500_mono_cam_0', 1), TOPIC)

    def test_wrong_message_type_rejected(self):
        with patch.object(verify.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, TOPIC, ''),
                subprocess.CompletedProcess([], 0, 'gz.msgs.CameraInfo', '')]):
            with self.assertRaises(RuntimeError):
                verify.discover('/gz', 'x500_camera', 'x500_mono_cam_0', 1)

    def test_bgr_capture_saves_rgb_snapshot(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 2] = 255
        with tempfile.TemporaryDirectory() as temp, patch.object(verify, 'frames', return_value=(f for f in [frame]*10)):
            result = verify.capture(TOPIC, 10, 1, Path(temp))
            self.assertEqual(result['count'], 10)
            self.assertEqual((Path(temp)/'frame.ppm').read_bytes()[-3:], b'\xff\x00\x00')

    def test_failed_live_frames_never_launch_tracker(self):
        with tempfile.TemporaryDirectory() as temp, patch('sys.argv', ['verify', '--output', temp, '--tracker-python', '/python']), patch.object(verify, 'discover', return_value=TOPIC), patch.object(verify, 'capture', side_effect=TimeoutError('no frames')), patch.object(verify.subprocess, 'call') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(), 1)
            launch.assert_not_called()
            self.assertFalse(json.loads((Path(temp)/'verification.json').read_text())['verified'])

    def test_launch_uses_discovered_topic_after_frames(self):
        with tempfile.TemporaryDirectory() as temp, patch('sys.argv', ['verify', '--output', temp, '--tracker-python', '/python']), patch.object(verify, 'discover', return_value=TOPIC), patch.object(verify, 'capture', return_value={'count': 10}), patch.object(verify.subprocess, 'call', return_value=0) as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(), 0)
            self.assertIn('gazebo://'+TOPIC, launch.call_args.args[0])
            report=json.loads((Path(temp)/'verification.json').read_text())
            self.assertTrue(report['verified'])
            self.assertFalse(report['qgc_ui_verified'])


if __name__ == '__main__':
    unittest.main()
