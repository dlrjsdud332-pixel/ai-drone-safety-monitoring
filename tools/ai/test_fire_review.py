"""Synthetic review workflow fixtures; never approve real dataset frames."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from prepare_fire_dataset import digest, verified, write
from prepare_fire_review import evaluate, fingerprint, import_review, merge_candidates


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.out=Path(self.temp.name)
        (self.out/'image.png').write_bytes(b'fixture identity only')
        self.row=dict(id='fixture',image='image.png',image_sha256=digest(self.out/'image.png'),
            label='labels_reviewed/fixture.txt',split='test',period='day',source_group='fixture',width=100,height=100,
            review=dict(status='pending'))
        self.manifest=dict(records=[self.row],cross_split_similarity_candidates=[])
        write(self.out/'manifest.json',self.manifest)
        self.review=dict(id='fixture',image_sha256=self.row['image_sha256'],status='completed',
            reviewer='SYNTHETIC TEST FIXTURE',complete_for_classes=['smoke','fire'],fire_present=True,
            completed_at='fixture',boxes=[[1,.5,.5,.2,.2],[0,.5,.3,.3,.2]],notes='not a real review')

    def tearDown(self):self.temp.cleanup()

    def submit(self,review):
        path=self.out/'submission.json'
        write(path,dict(schema_version=1,dataset_fingerprint=fingerprint(self.manifest),records=[review]))
        import_review(self.out,path)

    def test_pending_does_not_approve_or_mutate(self):
        before=(self.out/'manifest.json').read_bytes();self.review['status']='pending'
        with self.assertRaises(ValueError):self.submit(self.review)
        self.assertEqual(before,(self.out/'manifest.json').read_bytes())

    def test_explicit_completion_and_both_class_metrics(self):
        self.submit(self.review);manifest=json.loads((self.out/'manifest.json').read_text())
        self.assertEqual(len(verified(self.out,manifest)),1)
        write(self.out/'baseline_predictions.json',dict(weight_sha256='fixture',settings={},rows=[
            dict(id='fixture',image_sha256=self.row['image_sha256'],boxes=[
                dict(name='fire',confidence=.8,xyxy=[40,40,60,60])])]))
        evaluate(self.out);metrics=json.loads((self.out/'human_review_evaluation.json').read_text())['metrics']
        fire=next(m for m in metrics if (m['class_name'],m['split'],m['period'],m['confidence'])==('fire','test','day',.35))
        smoke=next(m for m in metrics if (m['class_name'],m['split'],m['period'],m['confidence'])==('smoke','test','day',.5))
        self.assertEqual((fire['tp'],fire['fn']),(1,0));self.assertEqual((smoke['tp'],smoke['fn']),(0,1))
        pending=copy.deepcopy(self.review);pending['status']='pending';self.submit(pending)
        self.assertEqual(len(verified(self.out,json.loads((self.out/'manifest.json').read_text()))),0)
        self.assertTrue((self.out/'labels_reviewed/fixture.txt').exists())

    def test_image_mismatch_is_rejected(self):
        self.review['image_sha256']='wrong'
        with self.assertRaises(ValueError):self.submit(self.review)

    def test_empty_negative_requires_explicit_completion(self):
        self.review.update(boxes=[],fire_present=False);self.submit(self.review)
        self.assertEqual((self.out/'labels_reviewed/fixture.txt').read_text(),'')


class CandidateTests(unittest.TestCase):
    def test_same_class_duplicate_merges_provenance(self):
        boxes,meta,pairs,count=merge_candidates([[1,.5,.5,.2,.2]],
            [dict(name='fire',confidence=.8,xyxy=[40,40,60,60])],100,100)
        self.assertEqual(len(boxes),1);self.assertEqual(count,1)
        self.assertEqual(meta[0]['sources'],['YOLO','partial_draft'])

    def test_nested_partial_and_distinct_class_preserved(self):
        boxes,meta,pairs,count=merge_candidates([[1,.5,.5,.1,.1]],
            [dict(name='fire',confidence=.8,xyxy=[30,30,70,70]),
             dict(name='smoke',confidence=.9,xyxy=[30,30,70,70])],100,100)
        self.assertEqual(len(boxes),3);self.assertEqual(count,0);self.assertEqual(pairs,[[0,2]])

    def test_empty_frame_retained(self):
        self.assertEqual(merge_candidates([],[],100,100),([],[],[],0))


if __name__=='__main__':unittest.main()
