"""Synthetic candidate-rule tests, not validated crowd safety labels."""
import unittest
from crowd_regions import CrowdRegions


def tracks(count,spread=False):
    result=[]
    for i in range(count):
        x=10+i*180 if spread else 300+(i%3)*25
        y=200+(i//3)*25
        result.append(dict(id=i+1,confidence=.9,xyxy=[x,y,x+20,y+60]))
    return result


class RegionTests(unittest.TestCase):
    def update_for(self,detector,people,seconds,zone=None,start=0):
        result=None
        for step in range(round(seconds*10)+1):result=detector.update(people,start+step/10,1920,1080,zone)
        return result

    def test_spread_eight_not_cluster(self):
        result=self.update_for(CrowdRegions(),tracks(8,True),4)
        self.assertEqual(result['level'],'NORMAL');self.assertEqual(result['unique_people'],8)

    def test_confirm_caution_then_high(self):
        d=CrowdRegions();result=self.update_for(d,tracks(6),1.5)
        self.assertEqual(result['level'],'CAUTION')
        result=self.update_for(d,tracks(6),1.5,start=1.5)
        self.assertEqual(result['level'],'HIGH');self.assertIsNone(result['physical_density'])

    def test_duplicate_ids_count_once(self):
        result=self.update_for(CrowdRegions(),tracks(2)*4,4)
        self.assertEqual(result['unique_people'],2);self.assertEqual(result['duplicate_ids_ignored'],6)
        self.assertEqual(result['level'],'NORMAL')

    def test_short_group_does_not_confirm(self):
        d=CrowdRegions();self.update_for(d,tracks(6),1)
        result=d.update([],3,1920,1080)
        self.assertEqual(result['level'],'NORMAL')

    def test_clear_and_reoccur(self):
        d=CrowdRegions();self.update_for(d,tracks(6),3)
        self.assertEqual(d.update([],4,1920,1080)['level'],'HIGH')
        self.assertEqual(d.update([],5.01,1920,1080)['level'],'NORMAL')
        result=self.update_for(d,tracks(6),3,start=6)
        self.assertEqual(result['level'],'HIGH')

    def test_zone_escalation_requires_group(self):
        zone=(0,0,1920,1080)
        result=self.update_for(CrowdRegions(),tracks(3),3,zone)
        self.assertEqual(result['level'],'HIGH')
        result=self.update_for(CrowdRegions(),tracks(3,True),3,zone)
        self.assertEqual(result['level'],'NORMAL')

    def test_out_of_order_time_rejected(self):
        d=CrowdRegions();d.update([],1,1920,1080)
        with self.assertRaises(ValueError):d.update([],0,1920,1080)

    def test_non_people_do_not_contribute(self):
        cars=[dict(p,class_id=2) for p in tracks(8)]
        result=self.update_for(CrowdRegions(),cars,4)
        self.assertEqual(result['unique_people'],0)


if __name__=='__main__':unittest.main()
