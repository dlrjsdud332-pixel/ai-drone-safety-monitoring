
import argparse
import json
import math
import socket
import threading
import time


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def move_toward(lat, lon, target_lat, target_lon, step_m):
    d = haversine_m(lat, lon, target_lat, target_lon)
    if d <= max(0.01, step_m):
        return target_lat, target_lon

    frac = step_m / d
    return (
        lat + (target_lat - lat) * frac,
        lon + (target_lon - lon) * frac,
    )


def bearing_deg(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    b = math.degrees(math.atan2(y, x))
    return (b + 360.0) % 360.0


class DroneSimulator:
    def __init__(self, args):
        self.args = args
        self.running = True

        self.lat = args.lat
        self.lon = args.lon
        self.home_lat = args.home_lat
        self.home_lon = args.home_lon
        self.altitude_m = args.altitude
        self.heading_deg = args.heading
        self.battery = args.battery
        self.flight_mode = "PATROL"

        self.target_lat = self.lat
        self.target_lon = self.lon
        self.target_alt = self.altitude_m

        self.telemetry_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.command_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.command_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.command_sock.bind(("127.0.0.1", args.command_port))
        self.command_sock.settimeout(0.2)

        self.lock = threading.Lock()
        self.last_command = "PATROL"
        self.last_command_id = None
        # Watchdog starts only after the first command from the controller.
        # This prevents an unattended simulator from immediately entering RTH at startup.
        self.last_command_at = None
        self.failsafe_state = "WAITING_FOR_CONTROLLER"
        self.failsafe_latched = False

    def command_loop(self):
        while self.running:
            try:
                data, _addr = self.command_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                cmd = json.loads(data.decode("utf-8"))
                if not isinstance(cmd, dict):
                    continue
            except Exception:
                continue

            name = str(cmd.get("command", "HOLD")).upper()
            params = cmd.get("params", {}) or {}

            with self.lock:
                if name not in {"PATROL", "TRACK", "ORBIT", "RTH", "LAND", "HOLD", "RECORD", "ALERT"}:
                    continue
                # Once autonomous RTH has started, a reconnect must not resume patrol.
                if self.failsafe_latched and name not in {"RTH", "LAND"}:
                    continue
                if self.flight_mode == "LANDED":
                    # Landing is terminal for this run; do not allow a new patrol.
                    continue
                self.last_command = name
                self.last_command_id = cmd.get("command_id")
                self.last_command_at = time.monotonic()
                if not self.failsafe_latched:
                    self.failsafe_state = "LINK_OK"

                if name == "PATROL":
                    self.flight_mode = "PATROL"
                    # Small patrol loop around HOME.
                    radius_m = 35.0
                    t = time.time() / 12.0
                    dlat = (radius_m / 111111.0) * math.sin(t)
                    dlon = (radius_m / (111111.0 * max(0.2, math.cos(math.radians(self.home_lat))))) * math.cos(t)
                    self.target_lat = self.home_lat + dlat
                    self.target_lon = self.home_lon + dlon
                    self.target_alt = float(params.get("altitude_m", self.args.altitude))

                elif name == "TRACK":
                    self.flight_mode = "TRACK"
                    self.target_alt = float(params.get("altitude_m", self.altitude_m))

                elif name == "ORBIT":
                    self.flight_mode = "ORBIT"
                    self.target_alt = float(params.get("altitude_m", self.altitude_m))

                elif name == "RTH":
                    self.flight_mode = "RETURNING_HOME"
                    self.target_lat = self.home_lat
                    self.target_lon = self.home_lon
                    self.target_alt = max(10.0, float(params.get("altitude_m", self.altitude_m)))

                elif name == "LAND":
                    self.flight_mode = "LAND"
                    self.target_lat = self.home_lat
                    self.target_lon = self.home_lon
                    self.target_alt = 0.0

                elif name == "HOLD":
                    self.flight_mode = "HOLD"
                    self.target_lat = self.lat
                    self.target_lon = self.lon
                    self.target_alt = self.altitude_m

                elif name in {"RECORD", "ALERT"}:
                    # Non-motion commands: keep current flight mode.
                    pass

            print(f"[SIM CMD] {name} | params={params}")

    def _check_link_failsafe(self, now):
        """Call with self.lock held. A command heartbeat is required after first command."""
        if self.flight_mode == "LANDED":
            return
        if self.last_command_at is None:
            return
        age = max(0.0, now - self.last_command_at)
        if age >= self.args.failsafe_rth_seconds and not self.failsafe_latched:
            self.failsafe_latched = True
            self.failsafe_state = "AUTO_RTH"
            self.flight_mode = "RETURNING_HOME"
            self.target_lat = self.home_lat
            self.target_lon = self.home_lon
            self.target_alt = max(10.0, self.altitude_m)
            print("\n[FAILSAFE] COMMAND LINK LOST -> autonomous RTH", flush=True)
        elif age >= self.args.failsafe_hold_seconds and not self.failsafe_latched:
            if self.failsafe_state != "AUTO_HOLD":
                self.failsafe_state = "AUTO_HOLD"
                if self.flight_mode not in {"RETURNING_HOME", "LAND"}:
                    self.flight_mode = "HOLD"
                    self.target_lat = self.lat
                    self.target_lon = self.lon
                    self.target_alt = self.altitude_m
                print("\n[FAILSAFE] COMMAND LINK LOST -> HOLD", flush=True)
        elif not self.failsafe_latched:
            self.failsafe_state = "LINK_OK"

    def update_motion(self, dt):
        with self.lock:
            self._check_link_failsafe(time.monotonic())
            speed = max(0.1, self.args.speed_mps)

            if self.flight_mode == "ORBIT":
                radius_m = 20.0
                t = time.time() / 5.0
                dlat = (radius_m / 111111.0) * math.sin(t)
                dlon = (radius_m / (111111.0 * max(0.2, math.cos(math.radians(self.home_lat))))) * math.cos(t)
                self.target_lat = self.home_lat + dlat
                self.target_lon = self.home_lon + dlon

            old_lat, old_lon = self.lat, self.lon
            self.lat, self.lon = move_toward(
                self.lat,
                self.lon,
                self.target_lat,
                self.target_lon,
                speed * dt,
            )

            moved = haversine_m(old_lat, old_lon, self.lat, self.lon)
            if moved > 0.01:
                self.heading_deg = bearing_deg(old_lat, old_lon, self.lat, self.lon)

            climb_rate = self.args.climb_mps
            dz = self.target_alt - self.altitude_m
            max_dz = climb_rate * dt
            if abs(dz) <= max_dz:
                self.altitude_m = self.target_alt
            else:
                self.altitude_m += max_dz if dz > 0 else -max_dz

            if self.flight_mode == "RETURNING_HOME":
                home_d = haversine_m(self.lat, self.lon, self.home_lat, self.home_lon)
                if home_d < 2.0:
                    self.flight_mode = "LAND"
                    self.target_alt = 0.0

            if self.flight_mode == "LAND" and self.altitude_m <= 0.2:
                self.altitude_m = 0.0
                self.flight_mode = "LANDED"

            # Simple battery drain.
            drain = self.args.battery_drain_per_min / 60.0 * dt
            if self.flight_mode not in {"LANDED"}:
                self.battery = max(0.0, self.battery - drain)

            if self.battery <= self.args.rth_battery and self.flight_mode not in {"LAND", "LANDED", "RETURNING_HOME"}:
                self.flight_mode = "RETURNING_HOME"
                self.target_lat = self.home_lat
                self.target_lon = self.home_lon

    def telemetry_packet(self):
        with self.lock:
            return {
                "battery": round(self.battery, 2),
                "lat": round(self.lat, 7),
                "lon": round(self.lon, 7),
                "home_lat": round(self.home_lat, 7),
                "home_lon": round(self.home_lon, 7),
                "altitude_m": round(self.altitude_m, 2),
                "heading_deg": round(self.heading_deg, 1),
                "flight_mode": self.flight_mode,
                "last_command": self.last_command,
                "last_command_id": self.last_command_id,
                "failsafe_state": self.failsafe_state,
                "failsafe_latched": self.failsafe_latched,
                "command_age_s": (round(time.monotonic() - self.last_command_at, 2)
                                  if self.last_command_at is not None else None),
            }

    def run(self):
        thread = threading.Thread(target=self.command_loop, daemon=True)
        thread.start()

        print(
            f"[DRONE SIM] telemetry -> 127.0.0.1:{self.args.telemetry_port} | "
            f"commands <- 127.0.0.1:{self.args.command_port}"
        )
        print(f"[DRONE SIM] command watchdog: HOLD after {self.args.failsafe_hold_seconds}s, "
              f"RTH after {self.args.failsafe_rth_seconds}s (starts after first command)")
        print("[DRONE SIM] Ctrl+C to stop")

        last = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                dt = max(0.001, now - last)
                last = now

                self.update_motion(dt)
                packet = self.telemetry_packet()
                payload = json.dumps(packet).encode("utf-8")
                self.telemetry_sock.sendto(payload, ("127.0.0.1", self.args.telemetry_port))

                home_d = haversine_m(packet["lat"], packet["lon"], packet["home_lat"], packet["home_lon"])
                print(
                    f"\r[SIM] MODE={packet['flight_mode']:<14} "
                    f"BAT={packet['battery']:>6.2f}% "
                    f"ALT={packet['altitude_m']:>6.1f}m "
                    f"HDG={packet['heading_deg']:>6.1f} "
                    f"HOME={home_d:>7.1f}m "
                    f"FS={packet['failsafe_state']:<12}   ",
                    end="",
                    flush=True,
                )
                time.sleep(1.0 / max(1.0, self.args.hz))

        except KeyboardInterrupt:
            print("\n[DRONE SIM] stopped")
        finally:
            self.running = False
            self.command_sock.close()
            self.telemetry_sock.close()


def main():
    p = argparse.ArgumentParser(description="Local UDP virtual drone for drone_tracker.py")
    p.add_argument("--telemetry-port", type=int, default=14560)
    p.add_argument("--command-port", type=int, default=14561)
    p.add_argument("--lat", type=float, default=37.5665)
    p.add_argument("--lon", type=float, default=126.9780)
    p.add_argument("--home-lat", type=float, default=37.5651)
    p.add_argument("--home-lon", type=float, default=126.9895)
    p.add_argument("--altitude", type=float, default=30.0)
    p.add_argument("--heading", type=float, default=90.0)
    p.add_argument("--battery", type=float, default=85.0)
    p.add_argument("--speed-mps", type=float, default=6.0)
    p.add_argument("--climb-mps", type=float, default=3.0)
    p.add_argument("--battery-drain-per-min", type=float, default=0.25)
    p.add_argument("--rth-battery", type=float, default=12.0)
    p.add_argument("--hz", type=float, default=5.0)
    p.add_argument("--failsafe-hold-seconds", type=float, default=3.0,
                   help="Seconds without a controller command before HOLD")
    p.add_argument("--failsafe-rth-seconds", type=float, default=10.0,
                   help="Seconds without a controller command before autonomous RTH")
    args = p.parse_args()
    if not (0 < args.failsafe_hold_seconds < args.failsafe_rth_seconds):
        p.error("Require 0 < --failsafe-hold-seconds < --failsafe-rth-seconds")
    DroneSimulator(args).run()


if __name__ == "__main__":
    main()
