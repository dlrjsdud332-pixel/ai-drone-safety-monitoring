"""Synthetic checks of evaluation and review gates, not model accuracy."""
import json
from pathlib import Path
import tempfile
import unittest

from prepare_fire_dataset import digest, evaluate, verified, write


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.out=Path(self.temp.name)
        self.manifest={'cross_split_similarity_candidates':[],'records':[]}
        self.predictions={'rows':[]}

    def tearDown(self): self.temp.cleanup()

    def sample(self,key,label,predictions,status='verified'):
        image=self.out/(key+'.png');image.write_bytes(b'test identity, not decoded for metric logic')
        labels=self.out/(key+'.txt');labels.write_text(label)
        row=dict(id=key,image=image.name,image_sha256=digest(image),label=labels.name,
            source_group=key,split='val',period='day',width=100,height=100,
            review=dict(status=status,reviewer='unit-test-fixture',complete_for_classes=['fire','smoke'],
                fire_present=bool(label),image_sha256=digest(image),label_sha256=digest(labels)))
        self.manifest['records'].append(row)
        self.predictions['rows'].append(dict(id=key,image_sha256=digest(image),boxes=predictions))

    def test_matching_misses_and_negative_false_alarm(self):
        self.sample('positive','1 .25 .25 .2 .2\n1 .75 .75 .2 .2\n',[
            dict(name='fire',confidence=.8,xyxy=[15,15,35,35]),
            dict(name='fire',confidence=.7,xyxy=[15,15,35,35])])
        self.sample('negative','',[dict(name='fire',confidence=.8,xyxy=[40,40,60,60])])
        write(self.out/'manifest.json',self.manifest);write(self.out/'baseline_predictions.json',self.predictions)
        evaluate(self.out)
        result=json.loads((self.out/'evaluation.json').read_text())['metrics'][1]
        self.assertEqual((result['tp'],result['fp'],result['fn']),(1,2,1))
        self.assertEqual(result['instance_miss_rate'],.5)
        self.assertEqual(result['negative_frame_false_alarm_rate'],1)

    def test_pending_labels_never_enter_metrics(self):
        self.sample('pending','',[],status='pending')
        write(self.out/'manifest.json',self.manifest);write(self.out/'baseline_predictions.json',self.predictions)
        evaluate(self.out)
        result=json.loads((self.out/'evaluation.json').read_text())
        self.assertEqual(result['status'],'insufficient_verified_ground_truth')
        self.assertIsNone(result['metrics'][1]['instance_miss_rate'])

    def test_changed_reviewed_label_is_rejected(self):
        self.sample('changed','',[])
        (self.out/'changed.txt').write_text('1 .5 .5 .2 .2\n')
        with self.assertRaises(ValueError):verified(self.out,self.manifest)


if __name__=='__main__':unittest.main()
