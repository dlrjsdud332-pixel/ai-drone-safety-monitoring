"""Offline checks for preserved flight setup, staged effects, and asset validation."""
import hashlib
import contextlib
import io
from types import SimpleNamespace
import subprocess
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import prepare_assets as assets

HERE=Path(__file__).resolve().parent
PROJECT=HERE.parents[2]


def fixture_glb():
    doc={'asset':{'version':'2.0'},'buffers':[{'byteLength':42}],
         'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36},{'buffer':0,'byteOffset':36,'byteLength':6}],
         'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3'},
                      {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}],
         'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1}]}]}
    header=json.dumps(doc).encode()
    header+=b' '*((-len(header))%4)
    binary=struct.pack('<9f3H',0,0,0,1,0,0,0,1,0,0,1,2)+b'\x00\x00'
    data=struct.pack('<II',len(header),0x4e4f534a)+header+struct.pack('<II',len(binary),0x004e4942)+binary
    return struct.pack('<4sII',b'glTF',2,12+len(data))+data


class IndustrialTests(unittest.TestCase):
    def test_originals_unchanged(self):
        baseline=json.loads((PROJECT/'diagnostics/gazebo/industrial/baseline-sha256.json').read_text())
        for name,digest in baseline.items():
            self.assertEqual(hashlib.sha256((PROJECT/name).read_bytes()).hexdigest(),digest,name)

    def test_stages_preserve_flight_and_have_one_worker(self):
        baseline=ET.parse(HERE.parent/'x500_camera.sdf').getroot().find('world')
        for filename in ['worker_only.sdf','industrial_site.sdf','industrial_effects.sdf']:
            world=ET.parse(HERE/filename).getroot().find('world')
            self.assertEqual(world.get('name'),baseline.get('name'))
            for tag in ['physics','gravity','magnetic_field','spherical_coordinates']:
                self.assertEqual(ET.canonicalize(ET.tostring(world.find(tag),encoding='unicode'),strip_text=True),ET.canonicalize(ET.tostring(baseline.find(tag),encoding='unicode'),strip_text=True))
            expected=next(i for i in baseline.findall('include') if i.findtext('uri')=='model://x500_mono_cam')
            actual=next(i for i in world.findall('include') if i.findtext('uri')=='model://x500_mono_cam')
            self.assertEqual(ET.canonicalize(ET.tostring(actual,encoding='unicode'),strip_text=True),ET.canonicalize(ET.tostring(expected,encoding='unicode'),strip_text=True))
            workers=[i for i in world.findall('include') if i.findtext('uri')=='model://industrial_worker']
            self.assertEqual(len(workers),1)
            self.assertFalse(any(m.get('name','').startswith('safety_') for m in world.findall('model')))
            self.assertEqual(world.find("plugin[@name='gz::sim::systems::Sensors']/render_engine").text,'ogre2')

    def test_worker_first_has_no_background_or_effects(self):
        world=ET.parse(HERE/'worker_only.sdf').getroot().find('world')
        self.assertEqual([m.get('name') for m in world.findall('model')],['ground_plane'])
        self.assertEqual(len(world.findall('.//particle_emitter')),0)
        effects=ET.parse(HERE/'industrial_effects.sdf').getroot()
        self.assertEqual(len(effects.findall('.//particle_emitter')),2)
        self.assertLessEqual(sum(float(e.findtext('rate'))*float(e.findtext('lifetime')) for e in effects.findall('.//particle_emitter')),132)

    def test_glb_content_and_budget(self):
        self.assertEqual(assets.inspect_glb(fixture_glb())['triangles_in_mesh_definitions'],1)
        for data in [b'<html>not an asset</html>',fixture_glb()[:-4]]:
            with self.assertRaises(ValueError):
                assets.inspect_glb(data)

    def test_check_rejects_missing_and_tampered_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            lock=root/'assets.lock.json'
            with patch.object(assets,'HERE',root),patch.object(assets,'LOCK',lock),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    assets.check()
                path=root/assets.CONFIG['worker']['path']
                data=fixture_glb()
                assets.install_file(path,data,assets.CONFIG['worker'],{})
                assets.check()
                path.write_bytes(data+b'tamper')
                with self.assertRaises(ValueError):
                    assets.check()


class MeasurementTests(unittest.TestCase):
    def test_failed_capture_replaces_previous_success(self):
        import measure_scene as measure
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            args=SimpleNamespace(label='worker',timeout=1,count=10)
            with patch.object(measure,'PROJECT',root),patch.object(measure,'OUTPUT',root/'benchmark'),patch.object(measure,'discover',side_effect=TimeoutError('no live topic')),contextlib.redirect_stdout(io.StringIO()):
                output=root/'benchmark/worker/capture.json'
                output.parent.mkdir(parents=True)
                output.write_text(json.dumps({'verified':True}))
                self.assertEqual(measure.capture(args),1)
                self.assertFalse(json.loads(output.read_text())['verified'])

    def test_capture_counts_after_warmup_and_reports_goal_separately(self):
        import measure_scene as measure
        import numpy as np
        frame=np.zeros((480,640,3),dtype=np.uint8)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            args=SimpleNamespace(label='worker',timeout=1,count=10)
            with patch.object(measure,'PROJECT',root),patch.object(measure,'OUTPUT',root/'benchmark'),patch.object(measure,'discover',return_value='/test/image'),patch.object(measure,'frames',return_value=(f for f in [frame]*30)),patch.object(measure.time,'monotonic',side_effect=[10+i*.2 for i in range(10)]),patch.object(measure.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"realTimeFactor":0.5}','')),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(measure.capture(args),0)
                report=json.loads((root/'benchmark/worker/capture.json').read_text())
                self.assertEqual(report['frames'],10)
                self.assertAlmostEqual(report['delivery_fps'],5)
                self.assertFalse(report['within_9_to_11_fps'])
                self.assertEqual(report['real_time_factor'],.5)


if __name__=='__main__':
    unittest.main()
