"""Controlled production crowd logic tests; synthetic detections, not accuracy."""
import contextlib
import io
from pathlib import Path
import tempfile
import time as clock
from types import SimpleNamespace
import unittest

import cv2
import numpy as np
from ultralytics.engine.results import Results
from replay_tracker import load_tracker


class CrowdTests(unittest.TestCase):
    def setUp(self):
        self.app=load_tracker();self.temp=tempfile.TemporaryDirectory();self.app.ROOT=Path(self.temp.name)
        self.now=1000.
        self.app.time=SimpleNamespace(time=lambda:self.now,monotonic=clock.monotonic,strftime=clock.strftime,sleep=clock.sleep)
        self.frame=np.full((480,640,3),127,np.uint8)
        self.channel=self.app.Channel(1,'',False,None);self.channel.source='synthetic-crowd';self.channel.show_fire_smoke=False
        self.analyzer=self.app.Analyzer([self.channel],'cpu');self.people=[]
        empty=Results(self.frame,path='synthetic',names={0:'person'},boxes=np.empty((0,6),np.float32))
        self.extra=SimpleNamespace(predict=lambda *a,**k:[empty])
        def track(*a,**k):
            return [Results(self.frame,path='synthetic',names={0:'person'},boxes=np.array(self.people,np.float32).reshape(-1,7))]
        self.model=SimpleNamespace(track=track)

    def tearDown(self):self.channel.stop();clock.sleep(.05);self.temp.cleanup()

    def check(self,n,spread=True,pending=None,ids=None,duplicate_boxes=False):
        self.people=[]
        for i in range(n):
            x=(i%4)*150+5 if spread else 220+(i%4)*15
            y=(i//4)*200+20 if spread else 200+(i//4)*15
            if duplicate_boxes:x,y=220,200
            self.people.append([x,y,x+10,y+30,(ids or list(range(1,n+1)))[i],.9,0])
        self.channel.frame_count=((self.channel.frame_count//10)+1)*10-1
        self.channel.publish(self.frame if pending is None else pending)
        with contextlib.redirect_stdout(io.StringIO()):
            self.analyzer.process(self.channel,self.model,self.extra,self.extra,None,self.frame)

    def test_threshold_seven_vs_eight_and_four_checks(self):
        for _ in range(4):self.check(7)
        self.assertEqual(self.channel.crowd_until,0)
        for _ in range(3):self.check(8)
        self.assertEqual(self.channel.crowd_until,0)
        self.check(8);self.assertEqual(self.channel.crowd_until,self.now+6)

    def test_spread_and_clustered_both_trigger(self):
        for spread in [False,True]:
            self.channel.crowd_hits=0;self.channel.crowd_until=0
            for _ in range(4):self.check(8,spread)
            self.assertEqual(self.channel.crowd_until,self.now+6)

    def test_zone_threshold_three_not_two(self):
        self.channel.danger=(0,0,640,480)
        for _ in range(4):self.check(2)
        self.assertEqual(self.channel.crowd_until,0)
        for _ in range(4):self.check(3)
        self.assertEqual(self.channel.crowd_until,self.now+6)

    def test_negative_decrements_not_strict_consecutive(self):
        for _ in range(3):self.check(8)
        self.check(7);self.assertEqual(self.channel.crowd_hits,2)
        self.check(8);self.check(8)
        self.assertEqual(self.channel.crowd_until,self.now+6)

    def test_expiry_without_new_inference(self):
        for _ in range(4):self.check(8)
        state=self.app.AgentState(self.app.TelemetryState())
        self.assertIn('CROWD',state.update([self.channel])['active_events'])
        self.now+=6.01
        self.assertNotIn('CROWD',state.update([self.channel])['active_events'])

    def test_duplicate_ids_collapse_and_duplicate_boxes_can_inflate(self):
        for _ in range(4):self.check(8,ids=[1,1,2,3,4,5,6,7])
        self.assertEqual(self.channel.crowd_until,0)
        # Eight IDs at one location are still eight counts; no spatial dedup here.
        self.channel.crowd_hits=0
        for _ in range(4):
            self.check(8,False,duplicate_boxes=True)
        self.assertGreater(self.channel.crowd_until,self.now)

    def test_cooldown_expiry_and_recurrence(self):
        manager=self.app.EventManager(self.app.TelemetryState());self.channel.event_manager=manager
        for _ in range(4):self.check(8)
        self.assertEqual(len(list(self.app.ROOT.glob('data/events/**/CROWD/*/event.json'))),1)
        self.now+=6.01;self.check(0)
        state=self.app.AgentState(manager.telemetry)
        self.assertNotIn('CROWD',state.update([self.channel])['active_events'])
        for _ in range(4):self.check(8)
        self.assertGreater(self.channel.crowd_until,self.now)
        self.assertEqual(len(list(self.app.ROOT.glob('data/events/**/CROWD/*/event.json'))),1)
        self.now=1030.02
        for _ in range(4):self.check(8)
        self.assertEqual(len(list(self.app.ROOT.glob('data/events/**/CROWD/*/event.json'))),2)

    def test_snapshot_is_the_crowd_inference_frame(self):
        manager=self.app.EventManager(self.app.TelemetryState());self.channel.event_manager=manager
        for _ in range(4):self.check(8,pending=np.zeros_like(self.frame))
        snapshot=next(self.app.ROOT.glob('data/events/**/CROWD/*/snapshot.jpg'))
        self.assertEqual(snapshot.read_bytes(),cv2.imencode('.jpg',self.frame)[1].tobytes())


if __name__=='__main__':unittest.main()
