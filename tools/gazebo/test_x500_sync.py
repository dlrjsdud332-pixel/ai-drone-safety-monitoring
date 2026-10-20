"""Pure offline regression tests for the read-only preflight decisions."""
import unittest
from check_x500_sync import assess


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.publishers=dict(clock=1,imu=1,barometer=1,camera=1)
        self.samples=dict(clock=[100_000_000,104_000_000,108_000_000],
                          imu=[100_000_000,104_000_000,108_000_000],
                          barometer=[100_000_000,120_000_000,140_000_000])

    def test_monotonic_single_source(self):
        self.assertEqual(assess('px4',self.publishers,self.samples),[])

    def test_existing_world_cannot_start_again(self):
        self.assertTrue(assess('gazebo',self.publishers,self.samples))
        self.assertEqual(assess('gazebo',dict.fromkeys(self.publishers,0),self.samples),[])

    def test_two_servers_rejected(self):
        self.publishers['clock']=2
        self.publishers['imu']=2
        self.assertTrue(assess('px4',self.publishers,self.samples))

    def test_clock_rewind_and_duplicate_imu_rejected(self):
        self.samples['clock']=[100_000_000,104_000_000,50_000_000]
        self.samples['imu']=[100_000_000,100_000_000,108_000_000]
        errors=assess('px4',self.publishers,self.samples)
        self.assertTrue(any('backwards' in e for e in errors))
        self.assertTrue(any('non-increasing' in e for e in errors))

    def test_missing_or_timed_out_baro_rejected(self):
        self.samples['barometer']=[]
        self.assertTrue(assess('px4',self.publishers,self.samples))
        self.samples['barometer']=[1,20_000_001,400_000_001]
        self.assertTrue(any('300ms' in e for e in assess('px4',self.publishers,self.samples)))


if __name__=='__main__':
    unittest.main()
