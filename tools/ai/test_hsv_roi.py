"""Geometry/selection checks only, not detector accuracy."""
import unittest
import numpy as np
from compare_hsv_roi import proposals, fuse, hits


class RoiTests(unittest.TestCase):
    def test_gray_image_has_no_color_proposals(self):
        frame=np.full((1080,1920,3),120,np.uint8)
        self.assertEqual(proposals(frame,2),([],0))

    def test_bright_patch_produces_bounded_context_crop(self):
        frame=np.zeros((1080,1920,3),np.uint8);frame[200:230,300:330]=(0,100,255)
        roi,count=proposals(frame,2)
        self.assertGreater(count,0);self.assertGreater(len(roi),0);self.assertLessEqual(len(roi),2)
        for r in roi:
            x,y,X,Y=r['xyxy'];self.assertTrue(0<=x<X<=1920 and 0<=y<Y<=1080)
            self.assertEqual(X-x,Y-y)

    def test_merge_deduplicates_same_class_only(self):
        a=dict(name='fire',confidence=.8,xyxy=[10,10,40,40]);b=dict(a,confidence=.7)
        c=dict(a,name='smoke',confidence=.9)
        self.assertEqual(len(fuse([a,b,c])),2)

    def test_patch_coverage_does_not_count_duplicate_boxes_twice(self):
        b=dict(name='fire',confidence=.5,xyxy=[10,10,40,40])
        self.assertEqual(hits([b,b],[[10,10,40,40]]),[.5])
        self.assertEqual(hits([dict(b,confidence=.34)],[[10,10,40,40]]),[0])


if __name__=='__main__':unittest.main()
