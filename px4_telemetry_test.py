"""PX4 SITL MAVLink telemetry read-only test (UDP port 14540).

Install: python -m pip install pymavlink
Run: python px4_telemetry_test.py
Stop: Ctrl+C
"""
import argparse
import math
import time
from pymavlink import mavutil


def main():
    parser = argparse.ArgumentParser(description='PX4 SITL MAVLink read-only telemetry')
    parser.add_argument('--port', type=int, default=14540, help='Local UDP receive port (PX4 onboard -> 14540)')
    parser.add_argument('--timeout', type=float, default=20.0, help='Seconds to wait for first heartbeat')
    args = parser.parse_args()

    link = mavutil.mavlink_connection(f'udpin:0.0.0.0:{args.port}', source_system=250)
    print(f'[PX4] Listening on UDP 0.0.0.0:{args.port} (read-only).', flush=True)
    print('[PX4] Leave PX4 SITL running. Ctrl+C to stop.', flush=True)
    first = link.wait_heartbeat(timeout=args.timeout)
    if first is None:
        print(f'[PX4] No heartbeat after {args.timeout:g}s. Check SITL MAVLink endpoint/port.', flush=True)
        return 1
    print(f'[PX4] HEARTBEAT received sysid={link.target_system} compid={link.target_component}', flush=True)
    latest = {}
    last_print = 0.0
    # No outgoing commands: consume only broadcast MAVLink messages.
    try:
        while True:
            msg = link.recv_match(blocking=True, timeout=1.0)
            if msg is not None and msg.get_type() != 'BAD_DATA':
                latest[msg.get_type()] = (msg, time.monotonic())
            now = time.monotonic()
            if now - last_print < 2.0:
                continue
            last_print = now
            hb = latest.get('HEARTBEAT', (first, now))[0]
            mode = mavutil.mode_string_v10(hb) if hb else 'UNKNOWN'
            armed = bool(hb.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED) if hb else False
            global_pos = latest.get('GLOBAL_POSITION_INT')
            gps = latest.get('GPS_RAW_INT')
            battery = latest.get('SYS_STATUS')
            batt_status = latest.get('BATTERY_STATUS')
            parts = [f'MODE={mode}', f'ARMED={armed}']
            if global_pos and now - global_pos[1] <= 5:
                p = global_pos[0]
                parts += [f'GPS={p.lat/1e7:.6f},{p.lon/1e7:.6f}', f'ALT_REL={p.relative_alt/1000:.1f}m', f'HDG={p.hdg/100:.1f}deg' if p.hdg != 65535 else 'HDG=--']
            elif gps and now - gps[1] <= 5:
                p = gps[0]
                parts += [f'GPS_RAW={p.lat/1e7:.6f},{p.lon/1e7:.6f}', 'ALT_REL=--']
            else:
                parts.append('GPS=WAITING')
            if batt_status and now - batt_status[1] <= 5 and batt_status[0].battery_remaining >= 0:
                parts.append(f'BAT={batt_status[0].battery_remaining}%')
            elif battery and now - battery[1] <= 5 and battery[0].battery_remaining >= 0:
                parts.append(f'BAT={battery[0].battery_remaining}%')
            else:
                parts.append('BAT=WAITING')
            print('[PX4] ' + ' | '.join(parts), flush=True)
    except KeyboardInterrupt:
        print('\n[PX4] Stopped.', flush=True)
    finally:
        link.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
