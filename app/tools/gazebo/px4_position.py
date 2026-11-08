"""Read-only PX4 position validity; link heartbeat is not a GPS fix."""
import math


def coordinates_valid(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and (abs(lat) > 1e-7 or abs(lon) > 1e-7))
    except (TypeError, ValueError):
        return False


def position_packet(message):
    kind = message.get_type()
    if kind == 'GPS_RAW_INT':
        return {'gps_fix_type': int(message.fix_type),
                'gps_satellites': int(message.satellites_visible),
                'gps_raw': {'lat': message.lat/1e7, 'lon': message.lon/1e7,
                            'alt_msl_m': message.alt/1000.}}
    if kind == 'GLOBAL_POSITION_INT':
        result = {'lat': message.lat/1e7, 'lon': message.lon/1e7,
                  'altitude_m': message.relative_alt/1000.,
                  'global_alt_msl_m': message.alt/1000.}
        if 0 <= message.hdg < 36000:
            result['heading_deg'] = message.hdg/100.
        return result
    if kind == 'HOME_POSITION':
        return {'home_lat': message.latitude/1e7, 'home_lon': message.longitude/1e7,
                'home_alt_msl_m': message.altitude/1000.}
    if kind == 'LOCAL_POSITION_NED':
        return {'local_position_ned': {k:float(getattr(message,k))
                                      for k in ('x','y','z','vx','vy','vz')}}
    return {}


class Px4Position:
    def __init__(self):
        self.latest = {}
        self.times = {}

    def ingest(self, packet, now):
        for group, trigger in (('fix','gps_fix_type'),('global','lat'),
                               ('home','home_lat'),('local','local_position_ned')):
            if trigger in packet:
                self.times[group] = float(now)
        self.latest.update(packet)

    def snapshot(self, now, connected=True):
        latest = self.latest
        fresh = lambda key: key in self.times and 0 <= now-self.times[key] <= 5.
        global_ok = coordinates_valid(latest.get('lat'),latest.get('lon'))
        raw = latest.get('gps_raw', {})
        fix_ok = (latest.get('gps_fix_type',0) >= 3
                  and coordinates_valid(raw.get('lat'),raw.get('lon')))
        gap = (math.hypot((float(latest['lat'])-float(raw['lat']))*111320.,
                         (float(latest['lon'])-float(raw['lon']))*111320.*math.cos(math.radians(float(raw['lat']))))
               if global_ok and fix_ok else None)
        valid = bool(connected and fresh('fix') and fresh('global') and global_ok and fix_ok
                     and gap is not None and gap <= 200.)
        home_ok = valid and coordinates_valid(latest.get('home_lat'),latest.get('home_lon'))
        status = ('VALID' if valid else 'STALE' if not connected or
                  (fix_ok and global_ok and not (fresh('fix') and fresh('global')))
                  else 'NO FIX' if not fix_ok else 'INVALID')
        return {'gps_valid':valid,'gps_status':status,'home_valid':bool(home_ok),
                'lat':float(latest['lat']) if valid else None,
                'lon':float(latest['lon']) if valid else None,
                'home_lat':float(latest['home_lat']) if home_ok else None,
                'home_lon':float(latest['home_lon']) if home_ok else None,
                'gps_fix_type':latest.get('gps_fix_type'),
                'gps_satellites':latest.get('gps_satellites'),
                'gps_raw':raw,'local_position_ned':latest.get('local_position_ned'),
                'raw_global_distance_m':gap,
                'gps_message_ages_s':{k:round(now-v,3) for k,v in self.times.items()}}
