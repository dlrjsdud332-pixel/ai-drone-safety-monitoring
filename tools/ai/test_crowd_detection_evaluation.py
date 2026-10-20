"""Check one-to-one target matching and refusal of unreviewed annotations."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from evaluate_crowd_detection import match, main


class EvaluationTests(unittest.TestCase):
    def test_duplicate_box_not_two_true_positives(self):
        box=dict(xyxy=[0,0,10,20],confidence=.8)
        self.assertEqual(match([box,dict(box,confidence=.7)],[[0,0,10,20]]),dict(tp=1,fp=1,fn=0))

    def test_missed_and_false_boxes_are_separate(self):
        self.assertEqual(match([dict(xyxy=[30,30,40,50],confidence=.9)],[[0,0,10,20]]),dict(tp=0,fp=1,fn=1))

    def test_unreviewed_predictions_are_not_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);labels=root/'labels.json';output=root/'metrics.json'
            labels.write_text(json.dumps(dict(frames=[dict(status='unreviewed',complete=True,confirmed_by='human')])))
            with patch.object(sys,'argv',['eval','--run',tmp,'--labels',str(labels),'--output',str(output)]),contextlib.redirect_stdout(io.StringIO()):main()
            self.assertEqual(json.loads(output.read_text())['human_confirmed_frames'],0)
            self.assertEqual(json.loads(output.read_text())['metrics'],{})


if __name__=='__main__':unittest.main()
