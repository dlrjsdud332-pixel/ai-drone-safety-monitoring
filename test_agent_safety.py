"""Run: python test_agent_safety.py
Standard-library-only unit tests for safety rules in legacy/drone_tracker.py.
This loads only the decision/validator classes via AST; it never starts a drone,
opens cameras, or sends UDP commands.
"""
import ast
import json
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACKER = ROOT / 'legacy' / 'drone_tracker.py'
if not TRACKER.is_file():
    raise SystemExit(f'File not found: {TRACKER}')

tree = ast.parse(TRACKER.read_text(encoding='utf-8'), filename=str(TRACKER))
names = {'AgentDecisionEngine', 'CommandSafetyValidator'}
selected = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
if {n.name for n in selected} != names:
    raise SystemExit('Required classes missing from drone_tracker.py')
module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
scope = {'json': json, 'threading': threading, 'time': time, 'BATTERY_RTH': 12}
exec(compile(module, str(TRACKER), 'exec'), scope)
Engine = scope['AgentDecisionEngine']
Validator = scope['CommandSafetyValidator']


def drone(**kw):
    d = dict(battery=85., flight_mode='PATROL', telemetry_source='UDP',
             telemetry_connected=True, telemetry_age_s=0.1,
             home_lat=37.5651, home_lon=126.9895, lat=37.5653, lon=126.9895,
             altitude_m=10., drone_profile=dict(max_horizontal_speed_mps=20.,
                 obstacle_avoid_speed_mps=16., max_ascent_speed_mps=6.,
                 max_descent_speed_mps=4.))
    d.update(kw)
    return d


def check(cmd='PATROL', d=None, speed=None):
    data = {'command': cmd, 'motion': cmd in ('PATROL','TRACK','ORBIT','RTH','LAND'),
            'params': {'speed_mps': speed} if speed is not None else {}}
    return Validator().validate(data, {'drone': d or drone()})


class AgentSafetyTests(unittest.TestCase):
    def test_normal_patrol(self):
        r = check()
        self.assertTrue(r['approved']); self.assertEqual(r['command'], 'PATROL')

    def test_udp_startup_without_telemetry(self):
        r = check(d=drone(telemetry_connected=False, telemetry_age_s=60))
        self.assertFalse(r['approved']); self.assertEqual(r['command'], 'HOLD')

    def test_udp_stale_telemetry(self):
        r = check('TRACK', drone(telemetry_age_s=6))
        self.assertFalse(r['approved']); self.assertEqual(r['reason'], 'UDP_TELEMETRY_LOST')

    def test_low_battery_rth_precedence(self):
        state={'drone': drone(battery=10), 'active_events':['FIRE']}
        decision=Engine().decide(state)
        self.assertEqual(decision['primary_action'], 'RTH')
        r=check('RTH', state['drone'])
        self.assertTrue(r['approved']); self.assertEqual(r['command'], 'RTH')

    def test_missing_home_blocks_regular_rth(self):
        r=check('RTH', drone(home_lat=None, home_lon=None))
        self.assertFalse(r['approved']); self.assertEqual(r['command'], 'HOLD')

    def test_missing_home_blocks_battery_override(self):
        r=check('PATROL', drone(battery=10, home_lat=None, home_lon=None))
        self.assertFalse(r['approved']); self.assertEqual(r['command'], 'HOLD')
        self.assertEqual(r['reason'], 'HOME_NOT_SET')

    def test_landed_never_restarts(self):
        r=check('RTH', drone(battery=10, flight_mode='LANDED'))
        self.assertEqual(r['command'], 'HOLD')
        self.assertEqual(r['reason'], 'ALREADY_LANDED')

    def test_speed_limited(self):
        r=check('PATROL', speed=100)
        self.assertLessEqual(r['safe_command']['params']['speed_mps'], 16)


if __name__ == '__main__':
    print('SAFETY TESTS: no cameras, networking, or real drone commands')
    unittest.main(verbosity=2)
