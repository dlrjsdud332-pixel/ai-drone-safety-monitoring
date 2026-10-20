"""Controlled detector-output tests of production alarm logic.

These synthetic boxes test counters/timers only, NOT actual model accuracy.
No simulation sockets, GUI, weights or production event files are used.
"""
import contextlib
import io
import time as real_time
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
from ultralytics.engine.results import Results
from replay_tracker import load_tracker


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.app=load_tracker()
        self.temp=tempfile.TemporaryDirectory()
        self.app.ROOT=Path(self.temp.name)
        self.now=1000.0
        self.app.time=SimpleNamespace(time=lambda:self.now,monotonic=lambda:self.now,strftime=real_time.strftime)
        self.channel=self.app.Channel(1,'',True,None)
        self.channel.source='controlled-alarm-test'
        self.channel.status='Playing'
        self.frame=np.full((480,640,3),127,np.uint8)
        self.empty=Results(self.frame,path='controlled',names={0:'person'},boxes=np.empty((0,6),np.float32))
        self.general=SimpleNamespace(track=lambda *a,**kw:[self.empty],predict=lambda *a,**kw:[self.empty])
        self.fire_conf=0.0;self.smoke_conf=0.0
        def predict(*a,**kw):
            boxes=[]
            if self.fire_conf:boxes.append([100,200,180,300,self.fire_conf,1])
            if self.smoke_conf:boxes.append([100,80,300,250,self.smoke_conf,0])
            return [Results(self.frame,path='controlled',names={0:'smoke',1:'fire'},boxes=np.array(boxes,np.float32).reshape(-1,6))]
        self.fire_model=SimpleNamespace(predict=predict)
        self.analyzer=self.app.Analyzer([self.channel],'cpu')

    def tearDown(self):
        self.channel.stop()
        self.temp.cleanup()

    def check_once(self,fire=0.0,smoke=0.0):
        self.fire_conf=fire;self.smoke_conf=smoke
        # Advance exactly one production inspection cycle.
        self.channel.frame_count=((self.channel.frame_count//10)+1)*10-1
        self.channel.publish(self.frame)
        self.analyzer.process(self.channel,self.general,self.general,self.general,self.fire_model,self.frame)

    def test_fire_two_hits_and_negative_decrement(self):
        self.check_once(fire=.8)
        self.assertEqual(self.channel.fire_hits,1)
        self.assertEqual(self.channel.fire_until,0)
        self.check_once()
        self.assertEqual(self.channel.fire_hits,0)
        self.check_once(fire=.8)
        self.check_once(fire=.8)
        self.assertEqual(self.channel.fire_until,self.now+10)
        self.assertEqual(self.channel.fire_hits,0)

    def test_smoke_four_checks_day_six_night(self):
        for _ in range(3):self.check_once(smoke=.8)
        self.assertEqual(self.channel.smoke_until,0)
        self.check_once(smoke=.8)
        self.assertEqual(self.channel.smoke_until,self.now+10)
        self.channel.smoke_until=0;self.channel.smoke_hits=0
        self.frame[:]=20
        for _ in range(5):self.check_once(smoke=.8)
        self.assertEqual(self.channel.smoke_until,0)
        self.check_once(smoke=.8)
        self.assertEqual(self.channel.smoke_until,self.now+10)

    def test_warning_and_agent_flags_expire_after_ten_seconds(self):
        for _ in range(4):self.check_once(fire=.8,smoke=.8)
        state=self.app.AgentState(self.app.TelemetryState())
        state.update([self.channel])
        self.assertIn('FIRE',state.snapshot()['active_events'])
        self.assertIn('SMOKE',state.snapshot()['active_events'])
        self.now+=10.01
        self.check_once()
        state.update([self.channel])
        self.assertNotIn('FIRE',state.snapshot()['active_events'])
        self.assertNotIn('SMOKE',state.snapshot()['active_events'])
        self.assertLess(self.channel.fire_until,self.now)
        self.assertLess(self.channel.smoke_until,self.now)

    def test_real_event_manager_cooldown_is_thirty_seconds(self):
        manager=self.app.EventManager(self.app.TelemetryState())
        self.channel.pending=self.frame
        # Use a non-video event to avoid asynchronous clip writers in a unit test.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(manager.emit(self.channel,'NO_HARDHAT',.8,track_id=1))
            self.now+=29.99
            self.assertFalse(manager.emit(self.channel,'NO_HARDHAT',.8,track_id=1))
            self.now+=.02
            self.assertTrue(manager.emit(self.channel,'NO_HARDHAT',.8,track_id=1))

    def test_agent_expiry_with_distinct_wall_and_monotonic_clocks(self):
        self.app.time.monotonic=lambda:50.0
        self.channel.fire_until=self.now+10
        self.channel.smoke_until=self.now+10
        state=self.app.AgentState(self.app.TelemetryState())
        self.assertIn('FIRE',state.update([self.channel])['active_events'])
        self.now+=10.01
        self.assertNotIn('FIRE',state.update([self.channel])['active_events'])
        self.assertNotIn('SMOKE',state.snapshot()['active_events'])

    def test_snapshot_uses_explicit_inference_frame(self):
        import cv2
        manager=self.app.EventManager(self.app.TelemetryState())
        self.channel.pending=np.zeros_like(self.frame)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(manager.emit(self.channel,'NO_HARDHAT',.8,frame=self.frame))
        snapshot=next(self.app.ROOT.glob('data/events/**/snapshot.jpg'))
        self.assertEqual(snapshot.read_bytes(),cv2.imencode('.jpg',self.frame)[1].tobytes())

    def test_thresholds_and_hit_count_is_not_strict_consecutive(self):
        self.check_once(fire=.34,smoke=.49)
        self.assertEqual(self.channel.fire_hits,0)
        self.assertEqual(self.channel.smoke_hits,0)
        self.check_once(smoke=.8);self.check_once(smoke=.8);self.check_once()
        self.assertEqual(self.channel.smoke_hits,1)
        self.assertEqual(self.channel.smoke_until,0)


if __name__=='__main__':
    unittest.main()
