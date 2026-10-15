from collections import deque, Counter
import argparse
import csv
import json
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path
import cv2
import numpy as np
import torch
import yt_dlp
from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "legacy" else HERE  # legacy 안에서도 기존 models/data 사용
WINDOW = "Drone Tracking"
WIDTH, HEIGHT, HEADER, FOOTER = 1440, 810, 70, 54
TOOLS = ("SELECT", "ZOOM", "MOVE", "RESET", "DANGER")
VEHICLES = (2, 3, 5, 7)
MAX_CAMERAS = 16

# ===== 추가 안전관제 설정 =====
EVENT_PRE_SECONDS = 5
EVENT_POST_SECONDS = 10
EVENT_CLIP_FPS = 5
EVENT_COOLDOWN = 30
FIRE_CONFIRM_HITS = 2
SMOKE_CONFIRM_HITS = 4
CROWD_CONFIRM_HITS = 4
PPE_CONFIRM_HITS = 3
INTRUSION_CONFIRM_HITS = 3
FALL_CONFIRM_HITS = 2

# 화면을 너무 복잡하게 만들지 않도록 일반 밀집 기준은 높이고,
# 위험구역 내부의 밀집만 더 민감하게 본다.
CROWD_SCREEN_THRESHOLD = 8
CROWD_DANGER_THRESHOLD = 3

# 저장 데이터 자동 정리
EVENT_RETENTION_DAYS = 7
MAX_EVENT_BUNDLES_PER_CAMERA = 30
MAX_QUEUE_FILES = 100

# 중요 이벤트만 영상까지 저장. PPE는 스냅샷+JSON만 저장한다.
VIDEO_EVENTS = {"FIRE", "SMOKE", "FALL", "INTRUSION", "CROWD", "LOW_BATTERY_RTH"}
BATTERY_WARN = 30
BATTERY_LIMIT = 20
BATTERY_RTH = 12

class DroneProfile:
    """기체별 성능/안전 한계를 한 곳에서 관리하는 기준 프로파일."""
    def __init__(
        self,
        key,
        display_name,
        max_horizontal_speed_mps,
        obstacle_avoid_speed_mps,
        max_ascent_speed_mps,
        max_descent_speed_mps,
        max_flight_time_min,
        max_hover_time_min,
        gnss_accuracy_m,
        vio_accuracy_m,
        obstacle_avoidance,
        max_gust_mps,
    ):
        self.key = key
        self.display_name = display_name
        self.max_horizontal_speed_mps = float(max_horizontal_speed_mps)
        self.obstacle_avoid_speed_mps = float(obstacle_avoid_speed_mps)
        self.max_ascent_speed_mps = float(max_ascent_speed_mps)
        self.max_descent_speed_mps = float(max_descent_speed_mps)
        self.max_flight_time_min = float(max_flight_time_min)
        self.max_hover_time_min = float(max_hover_time_min)
        self.gnss_accuracy_m = float(gnss_accuracy_m)
        self.vio_accuracy_m = float(vio_accuracy_m)
        self.obstacle_avoidance = str(obstacle_avoidance)
        self.max_gust_mps = float(max_gust_mps)

    def snapshot(self):
        return {
            "key": self.key,
            "display_name": self.display_name,
            "max_horizontal_speed_mps": self.max_horizontal_speed_mps,
            "obstacle_avoid_speed_mps": self.obstacle_avoid_speed_mps,
            "max_ascent_speed_mps": self.max_ascent_speed_mps,
            "max_descent_speed_mps": self.max_descent_speed_mps,
            "max_flight_time_min": self.max_flight_time_min,
            "max_hover_time_min": self.max_hover_time_min,
            "gnss_accuracy_m": self.gnss_accuracy_m,
            "vio_accuracy_m": self.vio_accuracy_m,
            "obstacle_avoidance": self.obstacle_avoidance,
            "max_gust_mps": self.max_gust_mps,
        }


DRONE_PROFILES = {
    # 실제 X10을 직접 제어한다는 뜻이 아니라, 관제/Agent 설계용 기준 성능값.
    "SKYDIO_X10_REF": DroneProfile(
        key="SKYDIO_X10_REF",
        display_name="Skydio X10 REF",
        max_horizontal_speed_mps=20.0,
        obstacle_avoid_speed_mps=16.0,
        max_ascent_speed_mps=6.0,
        max_descent_speed_mps=4.0,
        max_flight_time_min=40.0,
        max_hover_time_min=35.0,
        gnss_accuracy_m=1.0,
        vio_accuracy_m=0.10,
        obstacle_avoidance="360",
        max_gust_mps=12.8,
    ),
    "GENERIC": DroneProfile(
        key="GENERIC",
        display_name="Generic Drone",
        max_horizontal_speed_mps=10.0,
        obstacle_avoid_speed_mps=8.0,
        max_ascent_speed_mps=3.0,
        max_descent_speed_mps=2.0,
        max_flight_time_min=25.0,
        max_hover_time_min=20.0,
        gnss_accuracy_m=2.0,
        vio_accuracy_m=0.50,
        obstacle_avoidance="UNKNOWN",
        max_gust_mps=8.0,
    ),
}


class TelemetryState:
    """PX4/ROS2 연동 전까지 사용하는 공통 드론 상태 저장소."""
    def __init__(self, battery=100.0, lat=None, lon=None, home_lat=None, home_lon=None, drone_profile=None):
        self.lock = threading.Lock()
        self.drone_profile = drone_profile or DRONE_PROFILES["SKYDIO_X10_REF"]
        self.battery = float(battery)
        self.lat = lat
        self.lon = lon
        self.home_lat = home_lat if home_lat is not None else lat
        self.home_lon = home_lon if home_lon is not None else lon
        self.flight_mode = "PATROL"
        self.rth_requested = False
        self.rth_requested_at = None
        self.returning_home_at = None
        self.altitude_m = None
        self.heading_deg = None
        self.telemetry_source = "SIM"
        self.telemetry_connected = True
        self.telemetry_last_update = time.monotonic()

    def sim_heartbeat(self):
        """SIM 모드에서는 수동 입력이 없어도 텔레메트리가 살아있는 것으로 유지."""
        with self.lock:
            if self.telemetry_source == "SIM":
                self.telemetry_connected = True
                self.telemetry_last_update = time.monotonic()

    def snapshot(self):
        with self.lock:
            return {
                "battery": round(self.battery, 1),
                "lat": self.lat,
                "lon": self.lon,
                "home_lat": self.home_lat,
                "home_lon": self.home_lon,
                "flight_mode": self.flight_mode,
                "rth_requested": self.rth_requested,
                "altitude_m": self.altitude_m,
                "heading_deg": self.heading_deg,
                "telemetry_source": self.telemetry_source,
                "telemetry_connected": self.telemetry_connected,
                "telemetry_age_s": round(max(0.0, time.monotonic() - self.telemetry_last_update), 1),
                "drone_profile": self.drone_profile.snapshot(),
            }

    def update_battery(self, percent):
        """PX4/ROS2 연동 전 테스트용 배터리 갱신."""
        percent = float(percent)
        if not (0.0 <= percent <= 100.0):
            raise ValueError("battery must be between 0 and 100")
        with self.lock:
            self.battery = percent
            self.telemetry_source = "SIM"
            self.telemetry_connected = True
            self.telemetry_last_update = time.monotonic()
            # 테스트 중 배터리를 다시 올리면 RTH 시뮬레이션 상태도 초기화한다.
            if percent > BATTERY_RTH and self.rth_requested:
                self.rth_requested = False
                self.rth_requested_at = None
                self.returning_home_at = None
                self.flight_mode = "PATROL"

    def update_gps(self, lat, lon, set_home_if_empty=True):
        """PX4/ROS2 연동 전 테스트용 GPS 갱신."""
        lat = float(lat)
        lon = float(lon)
        if not (-90.0 <= lat <= 90.0):
            raise ValueError("latitude must be between -90 and 90")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError("longitude must be between -180 and 180")

        with self.lock:
            self.lat = lat
            self.lon = lon
            self.telemetry_source = "SIM"
            self.telemetry_connected = True
            self.telemetry_last_update = time.monotonic()
            if set_home_if_empty and (self.home_lat is None or self.home_lon is None):
                self.home_lat = lat
                self.home_lon = lon

    def update_altitude(self, altitude_m):
        """PX4/ROS2 연동 전 테스트용 고도 갱신."""
        altitude_m = float(altitude_m)
        with self.lock:
            self.altitude_m = altitude_m
            self.telemetry_source = "SIM"
            self.telemetry_connected = True
            self.telemetry_last_update = time.monotonic()

    def update_heading(self, heading_deg):
        """PX4/ROS2 연동 전 테스트용 기체 방향 갱신."""
        heading_deg = float(heading_deg) % 360.0
        with self.lock:
            self.heading_deg = heading_deg
            self.telemetry_source = "SIM"
            self.telemetry_connected = True
            self.telemetry_last_update = time.monotonic()

    def set_home_current(self):
        """현재 GPS 위치를 HOME으로 저장."""
        with self.lock:
            if self.lat is None or self.lon is None:
                return False
            self.home_lat = self.lat
            self.home_lon = self.lon
            return True

    def request_rth(self):
        with self.lock:
            if not self.rth_requested:
                self.rth_requested = True
                self.rth_requested_at = time.monotonic()
                self.returning_home_at = None
                self.flight_mode = "RTH_REQUESTED"
                return True
            return False

    def update_rth_simulation(self):
        """실제 PX4 연결 전 RTH 상태 흐름을 시간 기반으로 시뮬레이션."""
        with self.lock:
            if not self.rth_requested or self.rth_requested_at is None:
                return self.flight_mode

            now = time.monotonic()

            # 복귀 요청 후 잠시 명령 승인/전환 상태를 보여준다.
            if self.flight_mode == "RTH_REQUESTED" and now - self.rth_requested_at >= 2.0:
                self.flight_mode = "RETURNING_HOME"
                self.returning_home_at = now

            # 테스트에서는 5초간 복귀 중 상태를 보여준 뒤 착륙 상태로 전환한다.
            if (
                self.flight_mode == "RETURNING_HOME"
                and self.returning_home_at is not None
                and now - self.returning_home_at >= 5.0
            ):
                self.flight_mode = "LAND"

            return self.flight_mode

class TelemetryInputAdapter:
    """SIM/PX4/ROS2/MAVLink 입력을 TelemetryState 한 곳으로 모으는 어댑터."""
    def __init__(self, telemetry):
        self.telemetry = telemetry

    def ingest(self, packet, source="PX4"):
        """외부 텔레메트리 딕셔너리를 공통 상태로 반영."""
        with self.telemetry.lock:
            if "battery" in packet and packet["battery"] is not None:
                battery = float(packet["battery"])
                if 0.0 <= battery <= 100.0:
                    self.telemetry.battery = battery

            if packet.get("lat") is not None and packet.get("lon") is not None:
                lat = float(packet["lat"])
                lon = float(packet["lon"])
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    self.telemetry.lat = lat
                    self.telemetry.lon = lon
                    if self.telemetry.home_lat is None or self.telemetry.home_lon is None:
                        self.telemetry.home_lat = lat
                        self.telemetry.home_lon = lon

            if packet.get("home_lat") is not None:
                self.telemetry.home_lat = float(packet["home_lat"])
            if packet.get("home_lon") is not None:
                self.telemetry.home_lon = float(packet["home_lon"])

            if packet.get("altitude_m") is not None:
                self.telemetry.altitude_m = float(packet["altitude_m"])

            if packet.get("heading_deg") is not None:
                self.telemetry.heading_deg = float(packet["heading_deg"]) % 360.0

            if packet.get("flight_mode"):
                self.telemetry.flight_mode = str(packet["flight_mode"])

            self.telemetry.telemetry_source = str(source)
            self.telemetry.telemetry_connected = True
            self.telemetry.telemetry_last_update = time.monotonic()

    def disconnect(self, source=None):
        with self.telemetry.lock:
            self.telemetry.telemetry_connected = False
            if source:
                self.telemetry.telemetry_source = str(source)

    def test_battery(self, percent):
        self.telemetry.update_battery(percent)

    def test_gps(self, lat, lon):
        self.telemetry.update_gps(lat, lon, set_home_if_empty=True)

    def test_altitude(self, altitude_m):
        self.telemetry.update_altitude(altitude_m)

    def test_heading(self, heading_deg):
        self.telemetry.update_heading(heading_deg)


class EventManager:
    """탐지 결과를 FIRE/SMOKE/CROWD/FALL/INTRUSION/PPE 공통 이벤트로 기록."""
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.last_event = {}
        self.recent_event_locations = deque(maxlen=20)
        self.lock = threading.Lock()
        self.cleanup_storage()

    def cleanup_storage(self):
        """오래된 이벤트를 자동 정리하고 이벤트 종류별 폴더 구조를 유지한다."""
        cutoff = time.time() - EVENT_RETENTION_DAYS * 86400
        events_root = ROOT / "data" / "events"

        if events_root.exists():
            # 구조:
            # events/camera_N/EVENT_TYPE/EVENT_ID/{snapshot.jpg,event.json,event.mp4}
            for camera_dir in [p for p in events_root.iterdir() if p.is_dir()]:
                event_dirs = []

                for type_dir in [p for p in camera_dir.iterdir() if p.is_dir()]:
                    for event_dir in [p for p in type_dir.iterdir() if p.is_dir()]:
                        files = [p for p in event_dir.iterdir() if p.is_file()]
                        newest = max((p.stat().st_mtime for p in files), default=event_dir.stat().st_mtime)
                        event_dirs.append((newest, event_dir))

                        if newest < cutoff:
                            shutil.rmtree(event_dir, ignore_errors=True)

                    # 비어 있는 종류 폴더 제거
                    try:
                        if type_dir.exists() and not any(type_dir.iterdir()):
                            type_dir.rmdir()
                    except OSError:
                        pass

                # 카메라별 전체 이벤트 최근 N건만 유지
                remaining = [(t, p) for t, p in event_dirs if p.exists()]
                remaining.sort(reverse=True, key=lambda x: x[0])
                for _, event_dir in remaining[MAX_EVENT_BUNDLES_PER_CAMERA:]:
                    shutil.rmtree(event_dir, ignore_errors=True)

            # 비어 있는 camera 폴더 정리
            for camera_dir in [p for p in events_root.iterdir() if p.is_dir()]:
                try:
                    if not any(camera_dir.iterdir()):
                        camera_dir.rmdir()
                except OSError:
                    pass

        queue_dir = ROOT / "data" / "transmission_queue"
        if queue_dir.exists():
            queue_files = sorted(
                [p for p in queue_dir.glob("*.json") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in queue_files:
                if p.stat().st_mtime < cutoff:
                    try:
                        p.unlink()
                    except OSError:
                        pass

            queue_files = [p for p in queue_files if p.exists()]
            for p in queue_files[MAX_QUEUE_FILES:]:
                try:
                    p.unlink()
                except OSError:
                    pass

    def emit(self, channel, kind, confidence=1.0, track_id=None, details=None):
        now = time.time()
        key = (channel.number, kind, track_id)
        with self.lock:
            if now - self.last_event.get(key, 0.0) < EVENT_COOLDOWN:
                return False
            self.last_event[key] = now

        stamp = time.strftime("%Y%m%d_%H%M%S")
        event_id = f"{stamp}_{int((now % 1) * 1000):03d}"

        # 이벤트 종류별 + 이벤트 1건별 폴더로 분리
        # 예: data/events/camera_2/CROWD/20261012_171005_411/
        event_dir = ROOT / "data" / "events" / f"camera_{channel.number}" / kind / event_id
        event_dir.mkdir(parents=True, exist_ok=True)

        snapshot_path = event_dir / "snapshot.jpg"
        clip_path = event_dir / "event.mp4"
        meta_path = event_dir / "event.json"

        with channel.lock:
            frame = channel.pending.copy() if channel.pending is not None else (channel.raw.copy() if channel.raw is not None else None)
            pre_frames = [item[1].copy() for item in list(channel.event_buffer)]

        if frame is not None:
            cv2.imwrite(str(snapshot_path), frame)

        save_video = kind in VIDEO_EVENTS
        meta = {
            "event_id": event_id,
            "event": kind,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "camera": channel.number,
            "track_id": track_id,
            "confidence": round(float(confidence), 4),
            "telemetry": self.telemetry.snapshot(),
            "snapshot": str(snapshot_path),
            "video": str(clip_path) if save_video else None,
            "details": details or {},
            "transmission_status": "LOCAL_QUEUE",
            "metadata_note": "이 JSON은 이벤트 종류/시간/GPS/배터리/신뢰도 등 상황 정보를 저장합니다.",
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        event_telem = meta["telemetry"]
        if event_telem["lat"] is not None and event_telem["lon"] is not None:
            self.recent_event_locations.append(
                {
                    "event": kind,
                    "camera": channel.number,
                    "lat": float(event_telem["lat"]),
                    "lon": float(event_telem["lon"]),
                    "time": meta["time"],
                }
            )

        queue_dir = ROOT / "data" / "transmission_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        (queue_dir / f"{kind}_{event_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if save_video:
            threading.Thread(
                target=self._save_clip,
                args=(channel, pre_frames, clip_path),
                daemon=True,
            ).start()

        # 이벤트가 생길 때마다 용량 제한도 다시 확인
        self.cleanup_storage()
        print(f"[EVENT] {kind} | Cam {channel.number} | GPS=({meta['telemetry']['lat']}, {meta['telemetry']['lon']}) | {meta_path}")
        return True

    def _save_clip(self, channel, pre_frames, clip_path):
        frames = list(pre_frames)
        deadline = time.monotonic() + EVENT_POST_SECONDS
        next_sample = 0.0
        while time.monotonic() < deadline and not channel.stop_event.is_set():
            now = time.monotonic()
            if now >= next_sample:
                with channel.lock:
                    frame = channel.pending.copy() if channel.pending is not None else None
                if frame is not None:
                    frames.append(frame)
                next_sample = now + 1.0 / EVENT_CLIP_FPS
            time.sleep(0.01)

        if not frames:
            return
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(
            str(clip_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            EVENT_CLIP_FPS,
            (w, h),
        )
        if not writer.isOpened():
            print("[EVENT] 영상 저장 실패:", clip_path)
            return
        for frame in frames:
            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))
            writer.write(frame)
        writer.release()

class AgentState:
    """Vision AI + Drone Telemetry를 Agent가 읽기 쉬운 하나의 상태로 통합."""
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.lock = threading.Lock()
        self.state = {
            "ready": False,
            "active_events": [],
            "cameras": [],
            "drone": telemetry.snapshot(),
            "updated_at": time.time(),
        }

    def update(self, channels):
        now = time.monotonic()
        active_events = set()
        camera_states = []

        for c in channels:
            # 영상이 없는 슬롯도 구조에는 남기되 active 판단에서는 제외.
            has_source = bool(c.source)
            fire = has_source and now < c.fire_until
            smoke = has_source and now < c.smoke_until
            crowd = has_source and now < c.crowd_until
            fall = has_source and bool(c.fall_until)
            intrusion = has_source and bool(c.previous_danger)

            ppe_required = c.site_mode == "ON" or (
                c.site_mode == "AUTO" and c.danger is not None
            )
            no_hardhat = has_source and ppe_required and now < c.hardhat_until
            no_vest = has_source and ppe_required and now < c.vest_until

            event_flags = {
                "FIRE": fire,
                "SMOKE": smoke,
                "FALL": fall,
                "INTRUSION": intrusion,
                "CROWD": crowd,
                "NO_HARDHAT": no_hardhat,
                "NO_VEST": no_vest,
            }
            current = [name for name, enabled in event_flags.items() if enabled]
            active_events.update(current)

            camera_states.append(
                {
                    "camera": c.number,
                    "source_active": has_source,
                    "status": c.status,
                    "people": int(c.people),
                    "vehicles": int(c.vehicles),
                    "events": current,
                    "smoke_direction_screen": c.smoke_direction,
                    "night_mode": bool(c.night_mode),
                    "site_mode": c.site_mode,
                }
            )

        new_state = {
            "ready": True,
            "active_events": sorted(active_events),
            "cameras": camera_states,
            "drone": self.telemetry.snapshot(),
            "updated_at": time.time(),
        }

        with self.lock:
            self.state = new_state
        return new_state

    def snapshot(self):
        with self.lock:
            # 내부 리스트/딕셔너리를 외부에서 실수로 수정하지 않도록 JSON-safe 복사.
            return json.loads(json.dumps(self.state))


class AgentDecisionEngine:
    """AgentState를 읽고 다음 행동을 결정한다. 아직 실제 드론 명령은 보내지 않는다."""
    ACTIONS = ("PATROL", "TRACK", "ORBIT", "RECORD", "ALERT", "RTH", "LAND")

    def __init__(self):
        self.lock = threading.Lock()
        self.last_decision = {
            "primary_action": "PATROL",
            "actions": ["PATROL"],
            "reason": "NO_ACTIVE_EVENT",
            "priority": 0,
            "updated_at": time.time(),
        }

    def decide(self, agent_state):
        drone = agent_state.get("drone", {})
        events = set(agent_state.get("active_events", []))
        battery = float(drone.get("battery", 100.0))
        flight_mode = str(drone.get("flight_mode", "PATROL"))

        # 1) 안전 상태가 항상 최우선
        if flight_mode == "LAND":
            decision = {
                "primary_action": "LAND",
                "actions": ["LAND"],
                "reason": "RTH_SEQUENCE_LAND",
                "priority": 100,
            }
        elif battery <= BATTERY_RTH or flight_mode in ("RTH_REQUESTED", "RETURNING_HOME"):
            decision = {
                "primary_action": "RTH",
                "actions": ["ALERT", "RECORD", "RTH"],
                "reason": "LOW_BATTERY_OR_RTH_ACTIVE",
                "priority": 95,
            }

        # 2) 화재/연기: 현장 기록 + 경보 + ORBIT 후보
        elif "FIRE" in events:
            actions = ["ALERT", "RECORD", "ORBIT"]
            if "SMOKE" in events:
                reason = "FIRE_AND_SMOKE"
                priority = 90
            else:
                reason = "FIRE"
                priority = 88
            decision = {
                "primary_action": "ORBIT",
                "actions": actions,
                "reason": reason,
                "priority": priority,
            }

        # 3) 낙상: 사람 추적이 핵심
        elif "FALL" in events:
            decision = {
                "primary_action": "TRACK",
                "actions": ["ALERT", "RECORD", "TRACK"],
                "reason": "FALL",
                "priority": 85,
            }

        # 4) 위험구역 침입: 대상 추적 + 기록
        elif "INTRUSION" in events:
            decision = {
                "primary_action": "TRACK",
                "actions": ["ALERT", "RECORD", "TRACK"],
                "reason": "INTRUSION",
                "priority": 80,
            }

        # 5) 군중 밀집: 위치 유지하며 기록/경보
        elif "CROWD" in events:
            decision = {
                "primary_action": "RECORD",
                "actions": ["ALERT", "RECORD"],
                "reason": "CROWD",
                "priority": 70,
            }

        # 6) 연기만 감지: 이동보다는 우선 기록/경보
        elif "SMOKE" in events:
            decision = {
                "primary_action": "RECORD",
                "actions": ["ALERT", "RECORD"],
                "reason": "SMOKE",
                "priority": 65,
            }

        # 7) PPE: 비행 행동까지 바꾸지는 않고 관제 경보/기록
        elif events & {"NO_HARDHAT", "NO_VEST"}:
            missing = sorted(events & {"NO_HARDHAT", "NO_VEST"})
            decision = {
                "primary_action": "RECORD",
                "actions": ["ALERT", "RECORD"],
                "reason": "+".join(missing),
                "priority": 50,
            }

        else:
            decision = {
                "primary_action": "PATROL",
                "actions": ["PATROL"],
                "reason": "NO_ACTIVE_EVENT",
                "priority": 0,
            }

        decision["updated_at"] = time.time()

        with self.lock:
            previous = self.last_decision.get("primary_action")
            self.last_decision = decision
            if previous != decision["primary_action"]:
                print(
                    f"[AGENT] {previous} -> {decision['primary_action']} | "
                    f"{decision['reason']} | actions={','.join(decision['actions'])}"
                )
            return dict(decision)

    def snapshot(self):
        with self.lock:
            return dict(self.last_decision)


class DroneCommandBuilder:
    """Agent 결정 결과를 실제 비행 어댑터가 사용할 명령 형태로 변환한다.

    현재는 명령을 생성만 하며 실제 드론으로 전송하지 않는다.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.last_command = {
            "command": "PATROL",
            "motion": False,
            "params": {},
            "simulation": True,
            "updated_at": time.time(),
        }

    @staticmethod
    def _active_camera(agent_state):
        for camera in agent_state.get("cameras", []):
            if camera.get("events"):
                return camera.get("camera")
        return None

    def build(self, decision, agent_state):
        drone = agent_state.get("drone", {})
        profile = drone.get("drone_profile", {})
        action = decision.get("primary_action", "PATROL")
        camera = self._active_camera(agent_state)

        obstacle_speed = float(profile.get("obstacle_avoid_speed_mps", 8.0))
        ascent_speed = float(profile.get("max_ascent_speed_mps", 3.0))
        descent_speed = float(profile.get("max_descent_speed_mps", 2.0))

        current_alt = drone.get("altitude_m")
        default_alt = float(current_alt) if current_alt is not None else 30.0

        command = {
            "command": action,
            "motion": action in {"PATROL", "TRACK", "ORBIT", "RTH", "LAND"},
            "priority": int(decision.get("priority", 0)),
            "reason": decision.get("reason", ""),
            "camera": camera,
            "simulation": True,
            "params": {},
            "updated_at": time.time(),
        }

        if action == "PATROL":
            command["params"] = {
                "altitude_m": default_alt,
                "speed_mps": min(6.0, obstacle_speed * 0.5),
                "mode": "AREA_PATROL",
            }

        elif action == "TRACK":
            command["params"] = {
                "target_camera": camera,
                "altitude_m": default_alt,
                "speed_mps": min(8.0, obstacle_speed * 0.65),
                "follow_distance_m": 15.0,
            }

        elif action == "ORBIT":
            command["params"] = {
                "target_camera": camera,
                "altitude_m": default_alt,
                "radius_m": 20.0,
                "speed_mps": min(6.0, obstacle_speed * 0.5),
                "clockwise": True,
            }

        elif action == "RECORD":
            command["motion"] = False
            command["params"] = {
                "target_camera": camera,
                "record": True,
            }

        elif action == "ALERT":
            command["motion"] = False
            command["params"] = {
                "target_camera": camera,
                "send_alert": True,
            }

        elif action == "RTH":
            command["params"] = {
                "home_lat": drone.get("home_lat"),
                "home_lon": drone.get("home_lon"),
                "altitude_m": default_alt,
                "speed_mps": min(10.0, obstacle_speed * 0.75),
            }

        elif action == "LAND":
            command["params"] = {
                "descent_speed_mps": min(2.0, descent_speed),
                "target": "HOME_OR_CURRENT_SAFE_POINT",
            }

        # 기체 프로파일 한계를 명령에 명시해 나중 비행 어댑터가 재검증 가능하게 한다.
        command["limits"] = {
            "max_horizontal_speed_mps": profile.get("max_horizontal_speed_mps"),
            "obstacle_avoid_speed_mps": profile.get("obstacle_avoid_speed_mps"),
            "max_ascent_speed_mps": ascent_speed,
            "max_descent_speed_mps": descent_speed,
        }

        with self.lock:
            previous = self.last_command.get("command")
            self.last_command = command
            if previous != command["command"]:
                print(
                    f"[COMMAND] {previous} -> {command['command']} | "
                    f"camera={camera} | simulation=True"
                )
            return json.loads(json.dumps(command))

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.last_command))


class CommandSafetyValidator:
    """생성된 드론 명령을 기체/배터리/HOME/텔레메트리 상태 기준으로 재검증한다.

    현재는 실제 드론으로 보내기 전 단계의 시뮬레이션 안전 필터다.
    """
    MOTION_COMMANDS = {"PATROL", "TRACK", "ORBIT", "RTH", "LAND"}

    def __init__(self):
        self.lock = threading.Lock()
        self.last_result = {
            "approved": True,
            "original_command": "PATROL",
            "command": "PATROL",
            "reason": "SAFE",
            "adjustments": [],
            "updated_at": time.time(),
        }

    def validate(self, command, agent_state):
        drone = agent_state.get("drone", {})
        profile = drone.get("drone_profile", {})
        battery = float(drone.get("battery", 100.0))
        flight_mode = str(drone.get("flight_mode", "PATROL"))
        connected = bool(drone.get("telemetry_connected", True))
        telemetry_age = float(drone.get("telemetry_age_s", 0.0))

        original = json.loads(json.dumps(command))
        safe = json.loads(json.dumps(command))
        adjustments = []
        approved = True
        reason = "SAFE"

        cmd = str(safe.get("command", "PATROL"))
        params = safe.setdefault("params", {})

        max_speed = profile.get("max_horizontal_speed_mps")
        oa_speed = profile.get("obstacle_avoid_speed_mps")
        max_ascent = profile.get("max_ascent_speed_mps")
        max_descent = profile.get("max_descent_speed_mps")

        # 1) 이미 LAND 단계면 어떤 이동 명령보다 LAND가 우선.
        if flight_mode == "LAND":
            if cmd != "LAND":
                adjustments.append(f"{cmd}->LAND")
            safe["command"] = "LAND"
            safe["motion"] = True
            safe["params"] = {
                "descent_speed_mps": min(2.0, float(max_descent or 2.0)),
                "target": "HOME_OR_CURRENT_SAFE_POINT",
            }
            reason = "FLIGHT_MODE_LAND"

        # 2) 배터리 RTH 기준 이하에서는 PATROL/TRACK/ORBIT 금지.
        elif battery <= BATTERY_RTH and cmd not in {"RTH", "LAND"}:
            adjustments.append(f"{cmd}->RTH")
            safe["command"] = "RTH"
            safe["motion"] = True
            safe["params"] = {
                "home_lat": drone.get("home_lat"),
                "home_lon": drone.get("home_lon"),
                "altitude_m": drone.get("altitude_m") if drone.get("altitude_m") is not None else 30.0,
                "speed_mps": min(10.0, float(oa_speed or 8.0) * 0.75),
            }
            reason = "BATTERY_RTH_OVERRIDE"

        # 3) 텔레메트리 끊김/오래됨 시 새로운 일반 이동 명령 차단.
        elif cmd in {"PATROL", "TRACK", "ORBIT"} and (not connected or telemetry_age > 5.0):
            approved = False
            safe["command"] = "HOLD"
            safe["motion"] = False
            safe["params"] = {"hold_position": True}
            adjustments.append(f"{cmd}->HOLD")
            reason = "TELEMETRY_UNSAFE"

        # 4) RTH에 HOME 좌표가 없으면 자동 복귀 명령을 승인하지 않는다.
        elif cmd == "RTH" and (drone.get("home_lat") is None or drone.get("home_lon") is None):
            approved = False
            safe["command"] = "HOLD"
            safe["motion"] = False
            safe["params"] = {"hold_position": True, "alert": "HOME_NOT_SET"}
            adjustments.append("RTH->HOLD")
            reason = "HOME_NOT_SET"

        # 5) 속도를 DroneProfile 한계 안으로 강제 제한.
        if "speed_mps" in safe.get("params", {}):
            requested = float(safe["params"]["speed_mps"])
            speed_limit = float(oa_speed or max_speed or requested)
            if max_speed is not None:
                speed_limit = min(speed_limit, float(max_speed))
            limited = max(0.0, min(requested, speed_limit))
            if limited != requested:
                safe["params"]["speed_mps"] = limited
                adjustments.append(f"speed:{requested:.1f}->{limited:.1f}")

        # 6) 착륙 하강속도도 기체 한계 안으로 제한.
        if "descent_speed_mps" in safe.get("params", {}):
            requested = float(safe["params"]["descent_speed_mps"])
            limit = float(max_descent or requested)
            limited = max(0.1, min(requested, limit))
            if limited != requested:
                safe["params"]["descent_speed_mps"] = limited
                adjustments.append(f"descent:{requested:.1f}->{limited:.1f}")

        # 7) 명령 메타데이터에 검증 결과를 기록.
        safe["safety"] = {
            "approved": approved,
            "reason": reason,
            "adjustments": adjustments,
            "battery": battery,
            "telemetry_connected": connected,
            "telemetry_age_s": telemetry_age,
        }
        safe["updated_at"] = time.time()

        result = {
            "approved": approved,
            "original_command": original.get("command"),
            "command": safe.get("command"),
            "reason": reason,
            "adjustments": adjustments,
            "safe_command": safe,
            "updated_at": time.time(),
        }

        with self.lock:
            previous = self.last_result.get("command")
            self.last_result = result
            if previous != result["command"] or adjustments:
                print(
                    f"[SAFETY VALIDATOR] {original.get('command')} -> {result['command']} | "
                    f"approved={approved} | reason={reason} | "
                    f"adjustments={adjustments or 'none'}"
                )

        return json.loads(json.dumps(result))

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.last_result))


class DroneIOBridge:
    """실제 드론 연결부를 한 곳으로 모으는 공통 I/O 브리지.

    INPUT  : 실제 기체 -> TelemetryInputAdapter -> TelemetryState
    OUTPUT : safe_command -> FlightAdapter

    현재 SIM에서는 실제 외부 연결을 만들지 않고 구조만 사용한다.
    나중에 PX4/ROS2/Skydio 연결 코드는 이 클래스의 poll_telemetry()와
    FlightAdapter 구현부만 교체하면 된다.
    """
    def __init__(self, telemetry_input, flight_adapter):
        self.telemetry_input = telemetry_input
        self.flight_adapter = flight_adapter
        self.input_source = getattr(flight_adapter, "name", "SIM")
        self.last_input_at = None
        self.lock = threading.Lock()

    def ingest_telemetry(self, packet, source=None):
        """실제 어댑터가 받은 텔레메트리를 공통 상태로 전달."""
        src = source or self.input_source
        self.telemetry_input.ingest(packet, source=src)
        with self.lock:
            self.last_input_at = time.time()

    def poll_telemetry(self):
        """외부 기체 텔레메트리 수신 자리.

        SIM: 별도 수신 없음.
        PX4/ROS2/Skydio: 추후 SDK/MAVLink/ROS subscriber 등을 여기서 읽어
        ingest_telemetry()로 넘긴다.
        """
        return None

    def send_command(self, safe_command):
        """검증 완료 명령을 현재 FlightAdapter로 전달."""
        return self.flight_adapter.send(safe_command)

    def snapshot(self):
        with self.lock:
            return {
                "input_source": self.input_source,
                "last_input_at": self.last_input_at,
                "output_adapter": getattr(self.flight_adapter, "name", "UNKNOWN"),
                "adapter_connected": bool(getattr(self.flight_adapter, "connected", False)),
            }


class FlightAdapterBase:
    """모든 실제/시뮬레이션 비행 어댑터가 따르는 공통 인터페이스."""
    name = "BASE"

    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.last_sent = {
            "adapter": self.name,
            "connected": False,
            "status": "IDLE",
            "command": None,
            "ack": None,
            "sent_at": None,
        }

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def send(self, safe_command):
        raise NotImplementedError

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.last_sent))

    def _store_result(self, result):
        with self.lock:
            previous = (
                self.last_sent.get("status"),
                self.last_sent.get("command"),
                self.last_sent.get("ack"),
            )
            current = (result.get("status"), result.get("command"), result.get("ack"))
            self.last_sent = result
            if previous != current:
                print(
                    f"[FLIGHT ADAPTER] {result['adapter']} | "
                    f"{result['status']} | CMD={result['command']} | ACK={result['ack']}"
                )
        return json.loads(json.dumps(result))


class SimFlightAdapter(FlightAdapterBase):
    """검증된 safe_command를 실제 전송 없이 ACK 처리하는 시뮬레이션 어댑터."""
    name = "SIM"

    def __init__(self):
        super().__init__()
        self.connect()

    def send(self, safe_command):
        command_name = str(safe_command.get("command", "HOLD"))
        safety = safe_command.get("safety", {})
        approved = bool(safety.get("approved", True))

        if not self.connected:
            result = {
                "adapter": self.name,
                "connected": False,
                "status": "BLOCKED",
                "command": command_name,
                "ack": "ADAPTER_DISCONNECTED",
                "sent_at": time.time(),
            }
        elif not approved:
            result = {
                "adapter": self.name,
                "connected": True,
                "status": "BLOCKED",
                "command": command_name,
                "ack": safety.get("reason", "SAFETY_BLOCK"),
                "sent_at": time.time(),
            }
        else:
            result = {
                "adapter": self.name,
                "connected": True,
                "status": "ACK",
                "command": command_name,
                "ack": "SIM_ACCEPTED",
                "params": safe_command.get("params", {}),
                "sent_at": time.time(),
            }

        return self._store_result(result)


class Px4FlightAdapter(FlightAdapterBase):
    """PX4/MAVLink 연결용 자리. 실제 연결 전까지는 비활성 스텁."""
    name = "PX4"

    def send(self, safe_command):
        command_name = str(safe_command.get("command", "HOLD"))
        return self._store_result(
            {
                "adapter": self.name,
                "connected": self.connected,
                "status": "BLOCKED",
                "command": command_name,
                "ack": "PX4_NOT_CONNECTED",
                "sent_at": time.time(),
            }
        )


class Ros2FlightAdapter(FlightAdapterBase):
    """ROS2 연결용 자리. 실제 연결 전까지는 비활성 스텁."""
    name = "ROS2"

    def send(self, safe_command):
        command_name = str(safe_command.get("command", "HOLD"))
        return self._store_result(
            {
                "adapter": self.name,
                "connected": self.connected,
                "status": "BLOCKED",
                "command": command_name,
                "ack": "ROS2_NOT_CONNECTED",
                "sent_at": time.time(),
            }
        )


class SkydioFlightAdapter(FlightAdapterBase):
    """Skydio 개발자 도구/API 연결용 자리. 실제 연결 전까지는 비활성 스텁."""
    name = "SKYDIO"

    def send(self, safe_command):
        command_name = str(safe_command.get("command", "HOLD"))
        return self._store_result(
            {
                "adapter": self.name,
                "connected": self.connected,
                "status": "BLOCKED",
                "command": command_name,
                "ack": "SKYDIO_NOT_CONNECTED",
                "sent_at": time.time(),
            }
        )


def create_flight_adapter(name):
    name = str(name).upper()
    adapters = {
        "SIM": SimFlightAdapter,
        "PX4": Px4FlightAdapter,
        "ROS2": Ros2FlightAdapter,
        "SKYDIO": SkydioFlightAdapter,
    }
    if name not in adapters:
        raise ValueError(f"Unknown flight adapter: {name}")
    return adapters[name]()


class MissionExecutor:
    """Flight Adapter 결과를 받아 명령 실행 상태를 관리하고 Mission 로그를 저장한다.

    현재는 실제 기체가 없으므로 시간/비행상태 기반 SIM 실행 상태를 사용한다.
    Mission 상태가 바뀔 때마다 data/mission_logs 아래 JSONL + 개별 JSON으로 기록한다.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.last_signature = None
        self.started_at = None
        self.mission_id = None
        self.last_logged_status = None
        self.state = {
            "mission_id": None,
            "mission": "PATROL",
            "status": "RUNNING",
            "progress": 0,
            "reason": "INITIAL_PATROL",
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    @staticmethod
    def _signature(safe_command):
        return (
            safe_command.get("command"),
            safe_command.get("camera"),
            safe_command.get("reason"),
        )

    @staticmethod
    def _make_mission_id(command):
        return f"{time.strftime('%Y%m%d_%H%M%S')}_{int((time.time() % 1) * 1000):03d}_{command}"

    def _write_log(
        self,
        mission_state,
        safe_command,
        adapter_result,
        telemetry_state,
        agent_state=None,
        decision=None,
        validation=None,
    ):
        """상태 전환 시 Mission 전체 흐름을 JSON으로 남긴다."""
        day = time.strftime("%Y-%m-%d")
        root = ROOT / "data" / "mission_logs" / day
        root.mkdir(parents=True, exist_ok=True)

        record = {
            "mission_id": mission_state.get("mission_id"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mission": mission_state.get("mission"),
            "status": mission_state.get("status"),
            "progress": mission_state.get("progress"),
            "mission_reason": mission_state.get("reason"),
            "agent_events": (agent_state or {}).get("active_events", []),
            "agent_decision": decision or {},
            "safe_command": safe_command,
            "validation": validation or {},
            "adapter_result": adapter_result,
            "telemetry": telemetry_state,
        }

        # 하루 단위 append 로그
        jsonl_path = root / "mission_log.jsonl"
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 개별 Mission 최신 상태 파일
        mission_id = str(mission_state.get("mission_id") or "unknown")
        mission_path = root / f"{mission_id}.json"
        mission_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(
            f"[MISSION LOG] {mission_state.get('mission')} | "
            f"{mission_state.get('status')} | {mission_path}"
        )

    def _write_report(
        self,
        mission_state,
        safe_command,
        adapter_result,
        telemetry_state,
        agent_state=None,
        decision=None,
        validation=None,
    ):
        """Mission 종료/실패/보류 시 사람이 읽기 쉬운 요약 리포트를 저장."""
        status = str(mission_state.get("status", ""))
        if status not in {"COMPLETED", "FAILED", "HOLDING"}:
            return

        day = time.strftime("%Y-%m-%d")
        root = ROOT / "reports" / "mission_reports" / day
        root.mkdir(parents=True, exist_ok=True)

        mission_id = str(mission_state.get("mission_id") or "unknown")
        events = (agent_state or {}).get("active_events", [])
        decision_data = decision or {}
        validation_data = validation or {}

        summary = {
            "mission_id": mission_id,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "result": status,
            "detected_events": events,
            "agent_action": decision_data.get("primary_action"),
            "agent_reason": decision_data.get("reason"),
            "command": safe_command.get("command"),
            "command_params": safe_command.get("params", {}),
            "safety_approved": validation_data.get("approved"),
            "safety_reason": validation_data.get("reason"),
            "adapter": adapter_result.get("adapter"),
            "adapter_status": adapter_result.get("status"),
            "adapter_ack": adapter_result.get("ack"),
            "mission_progress": mission_state.get("progress"),
            "mission_reason": mission_state.get("reason"),
            "battery": telemetry_state.get("battery"),
            "gps": {
                "lat": telemetry_state.get("lat"),
                "lon": telemetry_state.get("lon"),
            },
            "home": {
                "lat": telemetry_state.get("home_lat"),
                "lon": telemetry_state.get("home_lon"),
            },
            "altitude_m": telemetry_state.get("altitude_m"),
            "heading_deg": telemetry_state.get("heading_deg"),
            "flight_mode": telemetry_state.get("flight_mode"),
            "drone_profile": (telemetry_state.get("drone_profile") or {}).get("display_name"),
        }

        json_path = root / f"{mission_id}_report.json"
        txt_path = root / f"{mission_id}_report.txt"

        json_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        event_text = ", ".join(events) if events else "NONE"
        lines = [
            "AI Drone Mission Report",
            "=" * 48,
            f"Mission ID: {mission_id}",
            f"Result: {status}",
            f"Detected Events: {event_text}",
            f"Agent Action: {summary['agent_action']}",
            f"Agent Reason: {summary['agent_reason']}",
            f"Command: {summary['command']}",
            f"Safety: {summary['safety_approved']} / {summary['safety_reason']}",
            f"Adapter: {summary['adapter']} / {summary['adapter_status']} / {summary['adapter_ack']}",
            f"Mission Progress: {summary['mission_progress']}%",
            f"Mission Reason: {summary['mission_reason']}",
            f"Battery: {summary['battery']}%",
            f"GPS: {summary['gps']['lat']}, {summary['gps']['lon']}",
            f"HOME: {summary['home']['lat']}, {summary['home']['lon']}",
            f"Altitude: {summary['altitude_m']} m",
            f"Heading: {summary['heading_deg']} deg",
            f"Flight Mode: {summary['flight_mode']}",
            f"Drone Profile: {summary['drone_profile']}",
        ]
        txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"[MISSION REPORT] {status} | {json_path}")
        print(f"[MISSION REPORT] TEXT | {txt_path}")

    def update(
        self,
        safe_command,
        adapter_result,
        telemetry_state,
        agent_state=None,
        decision=None,
        validation=None,
    ):
        now_mono = time.monotonic()
        now_wall = time.time()
        command = str(safe_command.get("command", "HOLD"))
        signature = self._signature(safe_command)
        adapter_status = str(adapter_result.get("status", "BLOCKED"))

        with self.lock:
            if signature != self.last_signature:
                self.last_signature = signature
                self.started_at = now_mono
                self.mission_id = self._make_mission_id(command)
                self.last_logged_status = None
                self.state = {
                    "mission_id": self.mission_id,
                    "mission": command,
                    "status": "QUEUED",
                    "progress": 0,
                    "reason": "NEW_COMMAND",
                    "started_at": now_wall,
                    "updated_at": now_wall,
                }
                print(
                    f"[MISSION] NEW -> {command} | "
                    f"id={self.mission_id} | status=QUEUED"
                )

            elapsed = 0.0 if self.started_at is None else max(0.0, now_mono - self.started_at)

            if adapter_status != "ACK":
                status = "FAILED"
                progress = 0
                reason = adapter_result.get("ack", "ADAPTER_BLOCKED")

            elif command == "PATROL":
                status = "RUNNING"
                progress = 0
                reason = "PATROL_ACTIVE"

            elif command == "HOLD":
                status = "HOLDING"
                progress = 0
                reason = "SAFE_HOLD"

            elif command in {"ORBIT", "TRACK"}:
                duration = 5.0
                progress = min(100, int((elapsed / duration) * 100))
                status = "COMPLETED" if progress >= 100 else "EXECUTING"
                reason = f"{command}_SIM"

            elif command == "RECORD":
                duration = 3.0
                progress = min(100, int((elapsed / duration) * 100))
                status = "COMPLETED" if progress >= 100 else "EXECUTING"
                reason = "RECORD_SIM"

            elif command == "RTH":
                flight_mode = str(telemetry_state.get("flight_mode", ""))
                if flight_mode == "LAND":
                    status = "COMPLETED"
                    progress = 100
                    reason = "RTH_REACHED_LAND"
                elif flight_mode == "RETURNING_HOME":
                    status = "EXECUTING"
                    progress = min(95, max(20, int(elapsed * 12)))
                    reason = "RETURNING_HOME"
                else:
                    status = "EXECUTING"
                    progress = min(20, int(elapsed * 10))
                    reason = "RTH_REQUESTED"

            elif command == "LAND":
                duration = 3.0
                progress = min(100, int((elapsed / duration) * 100))
                status = "COMPLETED" if progress >= 100 else "EXECUTING"
                reason = "LAND_SIM"

            else:
                status = "EXECUTING"
                progress = 0
                reason = "COMMAND_ACTIVE"

            previous_status = self.state.get("status")
            self.state.update(
                {
                    "mission_id": self.mission_id,
                    "mission": command,
                    "status": status,
                    "progress": progress,
                    "reason": reason,
                    "updated_at": now_wall,
                }
            )

            state_copy = json.loads(json.dumps(self.state))
            should_log = self.last_logged_status != status
            if should_log:
                self.last_logged_status = status

            if previous_status != status:
                print(
                    f"[MISSION] {command} | {previous_status} -> {status} | "
                    f"progress={progress}% | reason={reason}"
                )

        # 파일 쓰기는 lock 밖에서 수행하여 UI 지연을 줄인다.
        if should_log:
            self._write_log(
                state_copy,
                safe_command,
                adapter_result,
                telemetry_state,
                agent_state=agent_state,
                decision=decision,
                validation=validation,
            )
            self._write_report(
                state_copy,
                safe_command,
                adapter_result,
                telemetry_state,
                agent_state=agent_state,
                decision=decision,
                validation=validation,
            )

        return state_copy

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


class SafetyController:
    """Agent 이전 단계의 최소 안전 규칙. 실제 비행 명령은 아직 보내지 않음."""
    def __init__(self, telemetry, event_manager):
        self.telemetry = telemetry
        self.event_manager = event_manager
        self.last_level = None
        self.last_flight_mode = None

    def update(self, channel):
        # 실제 드론 연동 전에는 테스트용 RTH 상태 머신을 갱신한다.
        self.telemetry.update_rth_simulation()
        state = self.telemetry.snapshot()
        battery = state["battery"]

        if state["flight_mode"] != self.last_flight_mode:
            print(f"[FLIGHT] -> {state['flight_mode']}")
            self.last_flight_mode = state["flight_mode"]
        if battery <= BATTERY_RTH:
            level = "RTH"
        elif battery <= BATTERY_LIMIT:
            level = "LIMIT"
        elif battery <= BATTERY_WARN:
            level = "WARN"
        else:
            level = "OK"

        if level != self.last_level:
            print(f"[BATTERY] {battery:.1f}% -> {level}")
            self.last_level = level

        if level == "RTH" and self.telemetry.request_rth():
            self.event_manager.emit(
                channel,
                "LOW_BATTERY_RTH",
                confidence=1.0,
                details={
                    "battery": battery,
                    "action": "RTH_REQUESTED",
                    "next_states": ["RETURNING_HOME", "LAND"],
                    "simulation": True,
                },
            )


def smoke_screen_direction(dx, dy, min_move=18):
    """화면 좌표 기준 연기 이동 방향. GPS/heading 연동 전 단계."""
    distance = (dx * dx + dy * dy) ** 0.5
    if distance < min_move:
        return "STABLE"

    horizontal = ""
    vertical = ""

    if abs(dx) >= min_move * 0.55:
        horizontal = "RIGHT" if dx > 0 else "LEFT"
    if abs(dy) >= min_move * 0.55:
        vertical = "DOWN" if dy > 0 else "UP"

    if horizontal and vertical:
        return f"{horizontal}_{vertical}"
    return horizontal or vertical or "STABLE"

def smoke_screen_compass(direction):
    """화면 위쪽을 N으로 가정한 임시 나침반 값. 실제 heading 연동 전 UI용."""
    return {
        "UP": "N",
        "RIGHT_UP": "NE",
        "RIGHT": "E",
        "RIGHT_DOWN": "SE",
        "DOWN": "S",
        "LEFT_DOWN": "SW",
        "LEFT": "W",
        "LEFT_UP": "NW",
        "STABLE": "--",
        "UNKNOWN": "--",
    }.get(direction, "--")

def fit_image(image, width, height):
    h, w = image.shape[:2]
    interpolation = cv2.INTER_AREA if width < w or height < h else cv2.INTER_CUBIC
    canvas = cv2.resize(image, (width, height), interpolation=interpolation)  # 전체 영상을 잘림 없이 여백까지 채움
    return canvas, (0, 0, width, height)  # 가로·세로별 좌표 변환으로 클릭 위치 유지

def resolve_source(source):
    if "youtube.com/" in source or "youtu.be/" in source:
        options = {"quiet": True, "noplaylist": True, "format": "best[height<=720]/bestvideo[height<=720]/best", "socket_timeout": 10}
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(source, download=False)["url"]
    if source.isdecimal():
        return int(source)  # 0, 1 등: USB 카메라 번호
    if "://" not in source:
        path = Path(source).expanduser()
        return str(path if path.is_absolute() else ROOT / path)
    return source  # RTSP / HTTP 드론 스트림

class Channel:
    def __init__(self, number, source, ppe, event_manager=None):
        self.number, self.source, self.show_ppe = number, source, ppe
        self.event_manager = event_manager
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.pending, self.sequence, self.done = None, 0, -1
        self.overlay_mask, self.overlay_time, self.received_at = None, 0.0, 0.0
        self.receive_fps, self.receive_count, self.receive_start = 0.0, 0, time.monotonic()
        self.process_lock, self.process = threading.Lock(), None
        self.raw, self.image, self.objects = None, None, {}
        self.status, self.fps, self.people, self.vehicles = "Connecting", 0.0, 0, 0
        self.selected_id, self.roi, self.danger = None, None, None
        self.mode, self.drag_start, self.drag_end = "SELECT", None, None
        self.history, self.last_seen, self.upright, self.fall_until = {}, {}, {}, {}
        self.ppe_logged, self.danger_logged, self.previous_danger = {}, {}, set()
        self.hardhat_until, self.vest_until, self.frame_count = 0.0, 0.0, 0
        self.ppe_result, self.pose_result = None, None
        self.fire_result = None
        self.show_fire_smoke = True
        self.fire_hits = self.smoke_hits = self.crowd_hits = 0

        # SMOKE 이동 방향은 먼저 "화면 기준"으로 계산한다.
        # 실제 동/서/남/북은 추후 드론 heading(yaw)과 결합한다.
        self.smoke_centers = deque(maxlen=8)
        self.smoke_direction = "UNKNOWN"
        self.smoke_motion = 0.0
        self.smoke_anchor = None
        self.smoke_direction_history = deque(maxlen=7)

        # 밝기 기반 SMOKE 자동 보정
        self.scene_brightness = 255.0
        self.night_mode = False
        self.smoke_conf_threshold = 0.50
        self.smoke_box_area_limit = 0.25
        self.fire_anchor = None
        self.fire_anchor_until = 0.0
        self.fire_until = self.smoke_until = self.crowd_until = 0.0
        self.ppe_hits = {}
        self.intrusion_hits = {}
        self.fall_hits = {}
        self.site_mode = "AUTO"   # AUTO: 위험구역이 있을 때만 PPE 경고 / ON: 항상 / OFF: 끔
        self.event_buffer = deque(maxlen=EVENT_PRE_SECONDS * EVENT_CLIP_FPS)
        self.event_buffer_time = 0.0
        self.last_zoom, self.zoom_time = None, 0.0
        self.reader = threading.Thread(target=self.read_loop, daemon=True) if source else None
        if self.reader is not None:
            self.reader.start()
        else:
            self.status = "No source"
    def log(self, kind, track_id, item=None):
        folder = ROOT / "data" / "multi_tracker" / f"camera_{self.number}"
        folder.mkdir(parents=True, exist_ok=True)  # 영상별 CSV: 동일 ID가 섞이지 않음
        path = folder / f"{kind}_events.csv"
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(["time", "track_id"] + (["missing_item"] if item else []))
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), track_id] + ([item] if item else []))
    def publish(self, frame):
        now = time.monotonic()
        with self.lock:
            self.pending, self.sequence, self.status = frame, self.sequence + 1, "Playing"
            self.received_at = now
            if now - self.event_buffer_time >= 1.0 / EVENT_CLIP_FPS:
                self.event_buffer.append((now, frame.copy()))
                self.event_buffer_time = now
            self.receive_count += 1
            elapsed = now - self.receive_start
            if elapsed >= 1:
                self.receive_fps = self.receive_count / elapsed
                self.receive_count, self.receive_start = 0, now
    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()  # FFmpeg 읽기 대기도 즉시 해제
                except OSError:
                    pass
    def read_ffmpeg(self, url):
        command = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-rw_timeout", "15000000"]
        if url.startswith(("https://", "http://")):
            command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
        if ".m3u8" in url:
            command += ["-live_start_index", "-2"]  # 라이브 목록의 끝부분에서 시작
        if url.startswith("rtsp://"):
            command += ["-rtsp_transport", "tcp"]
        command += ["-re", "-i", url, "-an", "-sn", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        with self.process_lock:
            self.process = process
        finished = threading.Event()
        last_data = [time.monotonic()]
        def watchdog():
            while not finished.wait(0.5):
                if self.stop_event.is_set() or time.monotonic() - last_data[0] > 25:
                    if process.poll() is None:
                        try:
                            process.kill()  # 일정 시간 프레임이 없으면 새 주소로 재연결
                        except OSError:
                            pass
                    return
        guard = threading.Thread(target=watchdog, daemon=True)
        guard.start()
        try:
            frame_bytes = 1280 * 720 * 3
            while not self.stop_event.is_set():
                data = bytearray()
                while len(data) < frame_bytes:
                    chunk = process.stdout.read(frame_bytes - len(data))
                    if not chunk:
                        raise RuntimeError("FFmpeg stream disconnected")
                    data.extend(chunk)  # 파이프의 부분 수신을 한 프레임까지 모음
                last_data[0] = time.monotonic()
                self.publish(np.frombuffer(data, dtype=np.uint8).reshape(720, 1280, 3).copy())
        finally:
            finished.set()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            guard.join(timeout=1)
            with self.process_lock:
                if self.process is process:
                    self.process = None
    def read_loop(self):
        while not self.stop_event.is_set():
            cap = None
            try:
                resolved = resolve_source(self.source)
                local = isinstance(resolved, str) and "://" not in resolved
                if local and not Path(resolved).is_file():
                    raise FileNotFoundError(resolved)
                if isinstance(resolved, str) and "://" in resolved and shutil.which("ffmpeg"):
                    self.read_ffmpeg(resolved)
                    continue
                if isinstance(resolved, str) and "://" in resolved:
                    cap = cv2.VideoCapture(resolved, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 20000])
                else:
                    cap = cv2.VideoCapture(resolved)
                if not cap.isOpened():
                    raise RuntimeError("Cannot open source")
                fps = cap.get(cv2.CAP_PROP_FPS)
                interval = 1 / fps if local and 1 <= fps <= 240 else 0
                deadline = time.monotonic()
                while not self.stop_event.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        if local:
                            with self.lock:
                                self.status = "Ended"
                            return  # 파일 끝에서는 마지막 화면 유지
                        raise RuntimeError("Stream disconnected")
                    self.publish(frame)
                    if interval:
                        deadline += interval
                        self.stop_event.wait(max(0, deadline - time.monotonic()))
            except Exception as error:
                with self.lock:
                    self.status = "Connection error - retrying"
                print(f"[Camera {self.number}] {error}")
                self.stop_event.wait(3)
            finally:
                if cap is not None:
                    cap.release()  # 읽기 스레드에서만 해제: 다른 스레드와 충돌 방지

class Analyzer(threading.Thread):
    def __init__(self, channels, device):
        super().__init__(daemon=True)
        self.channels, self.device, self.stop_event = channels, device, threading.Event()
        self.error = None
    def run(self):
        try:
            models = {}  # 새 영상으로 바꾸면 추적기도 새로 생성
            pose = YOLO(str(ROOT / "models/yolo11n-pose.pt"))
            ppe = YOLO(str(ROOT / "models/ppe_best.pt"))  # P키로 나중에 켜도 모델을 사용할 수 있음
            fire_path = ROOT / "models" / "fire_smoke_best.pt"
            fire_smoke = YOLO(str(fire_path)) if fire_path.is_file() else None
            if fire_smoke is None:
                print("[INFO] models/fire_smoke_best.pt 없음 -> FIRE/SMOKE 모델 탐지는 비활성화")
            else:
                print("[INFO] FIRE/SMOKE 모델 활성화:", fire_path)
            while not self.stop_event.is_set():
                worked = False
                active_channels = list(self.channels)
                for old_channel in list(models):
                    if old_channel not in active_channels:
                        del models[old_channel]
                for channel in active_channels:
                    if self.stop_event.is_set():
                        break
                    with channel.lock:
                        frame, sequence = channel.pending, channel.sequence
                    if frame is None or sequence == channel.done:
                        continue
                    if channel not in models:
                        models[channel] = YOLO(str(ROOT / "models/yolo11s.pt"))
                    started = time.perf_counter()
                    self.process(channel, models[channel], pose, ppe, fire_smoke, frame)
                    channel.done = sequence
                    channel.fps = 1 / max(time.perf_counter() - started, 1e-6)
                    worked = True
                if not worked:
                    self.stop_event.wait(0.01)
        except Exception as error:
            self.error = str(error)
            print("AI 오류:", error)
    def process(self, c, model, pose, ppe, fire_smoke, frame):
        result = model.track(frame, device=self.device, persist=True, tracker="bytetrack.yaml", conf=0.35, iou=0.5, classes=[0, 2, 3, 5, 7], imgsz=832, verbose=False)[0]
        objects = {}
        for box in result.boxes:
            if box.id is not None:
                objects[int(box.id[0])] = (int(box.cls[0]), tuple(map(int, box.xyxy[0].tolist())))
        c.frame_count += 1
        inspect = c.frame_count % 10 == 0
        if inspect:
            # 장면 밝기를 이용해 야간/저조도에서 SMOKE 오탐을 자동으로 줄인다.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            c.scene_brightness = float(np.mean(gray))
            c.night_mode = c.scene_brightness < 70.0
            c.smoke_conf_threshold = 0.62 if c.night_mode else 0.50
            c.smoke_box_area_limit = 0.18 if c.night_mode else 0.25

            c.pose_result = pose.predict(frame, conf=0.50, imgsz=640, device=self.device, verbose=False)[0]
            c.ppe_result = ppe.predict(frame, conf=0.40, imgsz=640, classes=[0, 1, 2, 4], device=self.device, verbose=False)[0] if c.show_ppe else None
            c.fire_result = (
                fire_smoke.predict(frame, conf=0.30, imgsz=640, device=self.device, verbose=False)[0]
                if fire_smoke is not None and c.show_fire_smoke
                else None
            )
        if self.device == "mps":
            torch.mps.synchronize()  # 모든 모델 추론은 한 스레드에서 순서대로 실행
        image = frame.copy()

        # FIRE / SMOKE 시각화
        # 후보는 얇은 박스, 확정 이벤트 상태에서는 굵은 박스로 표시한다.
        if c.fire_result is not None:
            for box in c.fire_result.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])

                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                raw_name = str(c.fire_result.names.get(cls_id, cls_id))
                name = raw_name.lower().replace(" ", "_")

                # FIRE는 작은 불꽃도 놓치지 않도록 0.35부터 표시,
                # SMOKE는 기존처럼 0.50 이상만 표시.
                if ("fire" in name or "flame" in name) and conf < 0.35:
                    continue
                if "smoke" in name and conf < c.smoke_conf_threshold:
                    continue

                if "fire" in name or "flame" in name:
                    label = "FIRE"
                    color = (0, 0, 255)
                    confirmed = time.time() < c.fire_until
                elif "smoke" in name:
                    label = "SMOKE"
                    color = (0, 165, 255)
                    confirmed = time.time() < c.smoke_until
                else:
                    continue

                thickness = 3 if confirmed else 1
                prefix = "" if confirmed else "?"

                frame_h, frame_w = image.shape[:2]
                box_area = max(0, x2 - x1) * max(0, y2 - y1)
                frame_area = max(1, frame_w * frame_h)
                area_ratio = box_area / frame_area

                # FIRE는 기존처럼 박스를 유지.
                # SMOKE가 화면의 25% 이상이면 거대한 사각형은 생략하고
                # 중심점 + 라벨 + 방향 화살표만 표시한다.
                draw_box = not (
                    label == "SMOKE"
                    and area_ratio >= c.smoke_box_area_limit
                )

                if draw_box:
                    cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)

                    text_label = f"{prefix}{label} {conf:.2f}"
                    (tw, th), baseline = cv2.getTextSize(
                        text_label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2
                    )
                    ty = max(th + 6, y1)
                    cv2.rectangle(
                        image,
                        (x1, ty - th - 6),
                        (x1 + tw + 8, ty + baseline),
                        color,
                        -1,
                    )
                    cv2.putText(
                        image,
                        text_label,
                        (x1 + 4, ty - 3),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 255),
                        2,
                        getattr(cv2, "LINE_AA", 16),
                    )
                else:
                    # 너무 큰 SMOKE 박스는 화면을 가리지 않도록 중심점만 표시
                    cx = (x1 + x2) // 2
                    cy = (y1 + y2) // 2
                    cv2.circle(image, (cx, cy), 8, color, 2)

                    text_label = f"SMOKE {conf:.2f} | WIDE AREA"
                    cv2.putText(
                        image,
                        text_label,
                        (max(15, min(cx + 12, frame_w - 260)),
                         max(110, min(cy, frame_h - 20))),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        color,
                        2,
                        getattr(cv2, "LINE_AA", 16),
                    )

        # 연기 이동 방향 오버레이 (화면 기준)
        if (
            c.show_fire_smoke
            and c.smoke_direction not in ("UNKNOWN", "")
        ):
            # 중앙 화살표는 FIRE + SMOKE가 동시에 있을 때만 표시.
            # SMOKE only 상황에서는 중앙 화살표 없이 우측 나침반 방향만 보여준다.
            # 중앙 화살표는 현재 FIRE + SMOKE가 동시에 확인될 때만 표시한다.
            # 나침반은 마지막 유효 연기 방향을 유지한다.
            arrow_ready = (
                c.fire_anchor is not None
                and time.time() < c.fire_until
                and time.time() < c.smoke_until
                and c.smoke_direction not in ("UNKNOWN", "STABLE", "")
            )
            if arrow_ready:
                ax, ay = c.fire_anchor
            else:
                ax = ay = None

            arrow_vectors = {
                "RIGHT": (90, 0),
                "LEFT": (-90, 0),
                "UP": (0, -90),
                "DOWN": (0, 90),
                "RIGHT_UP": (70, -70),
                "RIGHT_DOWN": (70, 70),
                "LEFT_UP": (-70, -70),
                "LEFT_DOWN": (-70, 70),
                "STABLE": (0, 0),
            }
            vx, vy = arrow_vectors.get(c.smoke_direction, (0, 0))
            orange = (0, 165, 255)

            if arrow_ready:
                vx, vy = arrow_vectors.get(c.smoke_direction, (0, 0))
                if vx != 0 or vy != 0:
                    cv2.circle(image, (ax, ay), 6, (0, 0, 255), -1)
                    cv2.arrowedLine(
                        image,
                        (ax, ay),
                        (ax + vx, ay + vy),
                        (0, 165, 255),
                        3,
                        getattr(cv2, "LINE_AA", 16),
                        tipLength=0.28,
                    )

            compass = smoke_screen_compass(c.smoke_direction)

            # 오른쪽 위 나침반 UI
            # 실제 heading 연동 전까지는 화면 위쪽=N 기준의 임시 방향 표시.
            compass_center = (image.shape[1] - 74, 74)
            compass_radius = 44
            panel_x1 = image.shape[1] - 142
            panel_y1 = 10
            panel_x2 = image.shape[1] - 10
            panel_y2 = 138

            cv2.rectangle(
                image,
                (panel_x1, panel_y1),
                (panel_x2, panel_y2),
                (20, 20, 20),
                -1,
            )
            cv2.circle(
                image,
                compass_center,
                compass_radius,
                (210, 210, 210),
                1,
                getattr(cv2, "LINE_AA", 16),
            )

            label_positions = {
                "N": (compass_center[0] - 7, compass_center[1] - compass_radius - 8),
                "E": (compass_center[0] + compass_radius + 5, compass_center[1] + 6),
                "S": (compass_center[0] - 7, compass_center[1] + compass_radius + 18),
                "W": (compass_center[0] - compass_radius - 18, compass_center[1] + 6),
            }
            for cardinal, pos in label_positions.items():
                active = compass == cardinal
                color = (0, 165, 255) if active else (220, 220, 220)
                cv2.putText(
                    image,
                    cardinal,
                    pos,
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    color,
                    2 if active else 1,
                    getattr(cv2, "LINE_AA", 16),
                )

            # 대각선 방향까지 포함해 연기 진행 방향 화살표 표시
            compass_vectors = {
                "N": (0, -28),
                "NE": (20, -20),
                "E": (28, 0),
                "SE": (20, 20),
                "S": (0, 28),
                "SW": (-20, 20),
                "W": (-28, 0),
                "NW": (-20, -20),
            }
            cv2.circle(image, compass_center, 3, (255, 255, 255), -1)

            if compass in compass_vectors:
                cvx, cvy = compass_vectors[compass]
                cv2.arrowedLine(
                    image,
                    compass_center,
                    (compass_center[0] + cvx, compass_center[1] + cvy),
                    (0, 165, 255),
                    3,
                    getattr(cv2, "LINE_AA", 16),
                    tipLength=0.32,
                )

            cv2.putText(
                image,
                f"SMOKE {compass}",
                (panel_x1 + 14, panel_y2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (255, 255, 255),
                1,
                getattr(cv2, "LINE_AA", 16),
            )

        now = time.time()

        # FIRE / SMOKE: 단발 오탐을 줄이기 위해 일정 횟수 연속 검출 시 이벤트 확정
        if inspect and c.fire_result is not None:
            fire_conf = smoke_conf = 0.0
            largest_smoke = None
            largest_smoke_area = 0
            best_fire = None
            best_fire_score = -1.0

            for box in c.fire_result.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                name = str(c.fire_result.names.get(cls_id, cls_id)).lower().replace(" ", "_")

                if "fire" in name or "flame" in name:
                    fire_conf = max(fire_conf, conf)
                    fx1, fy1, fx2, fy2 = map(int, box.xyxy[0].tolist())
                    fire_area = max(1, fx2 - fx1) * max(1, fy2 - fy1)
                    fire_score = conf * (fire_area ** 0.35)
                    if fire_score > best_fire_score:
                        best_fire_score = fire_score
                        best_fire = (fx1, fy1, fx2, fy2)

                if "smoke" in name and conf >= c.smoke_conf_threshold:
                    smoke_conf = max(smoke_conf, conf)
                    sx1, sy1, sx2, sy2 = map(int, box.xyxy[0].tolist())
                    area = max(0, sx2 - sx1) * max(0, sy2 - sy1)
                    if area > largest_smoke_area:
                        largest_smoke_area = area
                        largest_smoke = (sx1, sy1, sx2, sy2)

            if best_fire is not None:
                fx1, fy1, fx2, fy2 = best_fire
                # 화염 박스의 하단 중앙을 현재 화재 시작점으로 사용한다.
                c.fire_anchor = ((fx1 + fx2) // 2, fy2)
                c.fire_anchor_until = now + 2.0
            elif time.time() >= c.fire_anchor_until:
                c.fire_anchor = None

            # 연기 방향 계산:
            # FIRE가 있으면 "불 시작점 -> 연기 중심"을 우선 사용한다.
            # FIRE가 한 번도 잡히지 않은 SMOKE-only 상황에서는
            # 여러 시점의 연기 중심 이동을 이용해 방향만 계산한다.
            if largest_smoke is not None:
                sx1, sy1, sx2, sy2 = largest_smoke
                center = ((sx1 + sx2) // 2, (sy1 + sy2) // 2)
                c.smoke_centers.append(center)
                c.smoke_anchor = center

                new_direction = "UNKNOWN"
                new_motion = 0.0

                if c.fire_anchor is not None:
                    dx = center[0] - c.fire_anchor[0]
                    dy = center[1] - c.fire_anchor[1]
                    new_direction = smoke_screen_direction(dx, dy, min_move=35)
                    new_motion = (dx * dx + dy * dy) ** 0.5
                elif len(c.smoke_centers) >= 4:
                    pts = list(c.smoke_centers)
                    start_x = sum(p[0] for p in pts[:2]) / 2.0
                    start_y = sum(p[1] for p in pts[:2]) / 2.0
                    end_x = sum(p[0] for p in pts[-2:]) / 2.0
                    end_y = sum(p[1] for p in pts[-2:]) / 2.0
                    dx = end_x - start_x
                    dy = end_y - start_y
                    new_direction = smoke_screen_direction(dx, dy, min_move=22)
                    new_motion = ((dx * dx + dy * dy) ** 0.5) / max(1, len(pts) - 2)

                # 방향 안정화:
                # 최근 7회의 후보를 모아 다수결로 결정하고,
                # 현재 방향의 정반대 방향으로 갑자기 튀는 값은 더 강한 근거가 있을 때만 변경한다.
                if new_direction not in ("UNKNOWN", "STABLE", ""):
                    c.smoke_direction_history.append(new_direction)
                    votes = Counter(c.smoke_direction_history)
                    voted_direction, voted_count = votes.most_common(1)[0]

                    opposite = {
                        "RIGHT": "LEFT",
                        "LEFT": "RIGHT",
                        "UP": "DOWN",
                        "DOWN": "UP",
                        "RIGHT_UP": "LEFT_DOWN",
                        "LEFT_DOWN": "RIGHT_UP",
                        "RIGHT_DOWN": "LEFT_UP",
                        "LEFT_UP": "RIGHT_DOWN",
                    }

                    current = c.smoke_direction
                    is_reverse_jump = (
                        current not in ("UNKNOWN", "STABLE", "")
                        and opposite.get(current) == voted_direction
                    )

                    # 일반 방향 변경은 최근 3표 이상,
                    # 180도 반전은 최근 5표 이상일 때만 허용.
                    required_votes = 5 if is_reverse_jump else 3
                    if voted_count >= required_votes or current in ("UNKNOWN", "STABLE", ""):
                        c.smoke_direction = voted_direction
                        c.smoke_motion = new_motion
            else:
                # 현재 FIRE/SMOKE 감지가 끊겨도 마지막 나침반 방향은 유지한다.
                pass

            c.fire_hits = c.fire_hits + 1 if fire_conf >= 0.35 else max(0, c.fire_hits - 1)

            smoke_required_hits = 6 if c.night_mode else SMOKE_CONFIRM_HITS
            smoke_ok = smoke_conf >= c.smoke_conf_threshold
            c.smoke_hits = c.smoke_hits + 1 if smoke_ok else max(0, c.smoke_hits - 1)

            if c.fire_hits >= FIRE_CONFIRM_HITS:
                c.fire_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(
                        c, "FIRE", fire_conf,
                        details={"confirmed_hits": c.fire_hits}
                    )
                c.fire_hits = 0

            if c.smoke_hits >= smoke_required_hits:
                c.smoke_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(
                        c,
                        "SMOKE",
                        smoke_conf,
                        details={
                            "confirmed_hits": c.smoke_hits,
                            "screen_direction": c.smoke_direction,
                            "screen_compass": smoke_screen_compass(c.smoke_direction),
                            "screen_motion_px_per_check": round(c.smoke_motion, 2),
                            "scene_brightness": round(c.scene_brightness, 1),
                            "night_mode": c.night_mode,
                            "smoke_conf_threshold": round(c.smoke_conf_threshold, 2),
                            "compass_direction": None,
                            "heading_required": True,
                        },
                    )
                c.smoke_hits = 0

        if inspect and c.pose_result is not None:
            for coords in c.pose_result.boxes.xyxy:
                x1, y1, x2, y2 = map(float, coords.tolist())
                best_id, best_iou = None, 0
                for tid, (kind, (bx1, by1, bx2, by2)) in objects.items():
                    if kind != 0:
                        continue
                    intersection = max(0, min(x2, bx2) - max(x1, bx1)) * max(0, min(y2, by2) - max(y1, by1))
                    overlap = intersection / ((x2 - x1) * (y2 - y1) + (bx2 - bx1) * (by2 - by1) - intersection + 1e-6)
                    if overlap > best_iou:
                        best_id, best_iou = tid, overlap
                if best_iou < 0.3:
                    continue
                if x2 - x1 > (y2 - y1) * 1.8 and c.upright.get(best_id, False):
                    c.fall_hits[best_id] = c.fall_hits.get(best_id, 0) + 1
                    if c.fall_hits[best_id] >= FALL_CONFIRM_HITS:
                        c.log("fall", best_id)
                        if c.event_manager:
                            c.event_manager.emit(c, "FALL", 1.0, track_id=best_id)
                        c.fall_until[best_id], c.upright[best_id] = now + 10, False
                        c.fall_hits[best_id] = 0
                elif y2 - y1 > (x2 - x1) * 1.2:
                    c.upright[best_id] = True
                    c.fall_hits[best_id] = 0
        if c.show_ppe and c.ppe_result is not None:
            image = c.ppe_result.plot(img=image, labels=True, conf=True, line_width=1)
        with c.lock:
            danger = c.danger
        inside = set()
        for tid, (kind, rect) in objects.items():
            x1, y1, x2, y2 = rect
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            c.last_seen[tid] = now
            points = c.history.setdefault(tid, [])
            points.append(center)
            del points[:-60]
            name = "HUMAN" if kind == 0 else "VEHICLE" if kind in VEHICLES else "OBJECT"
            color = (0, 255, 0) if kind == 0 else (255, 160, 0) if kind in VEHICLES else (0, 255, 255)
            if kind == 0 and danger is not None:
                dx1, dy1, dx2, dy2 = danger
                if dx1 <= center[0] <= dx2 and dy1 <= y2 <= dy2:
                    inside.add(tid)
                    color = (0, 0, 255)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 1)
            cv2.putText(image, f"{name} ID:{tid}", (max(0, x1), max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
            ppe_required = c.site_mode == "ON" or (c.site_mode == "AUTO" and danger is not None)
            if kind == 0 and inspect and c.show_ppe and ppe_required and c.ppe_result is not None:
                missing = set()
                for box in c.ppe_result.boxes:
                    item = int(box.cls[0])
                    if item not in (1, 2) or float(box.conf[0]) < 0.5:
                        continue
                    px1, py1, px2, py2 = map(float, box.xyxy[0].tolist())
                    if x1 <= (px1 + px2) / 2 <= x2 and y1 <= (py1 + py2) / 2 <= y2:
                        missing.add(item)
                # 후보 상태는 파일로 저장하지 않는다.
                # 같은 Track ID에서 여러 검사 주기 연속 확인될 때만 확정 이벤트 생성.
                for item in (1, 2):
                    key = (tid, item)
                    if item in missing:
                        c.ppe_hits[key] = c.ppe_hits.get(key, 0) + 1
                    else:
                        c.ppe_hits[key] = max(0, c.ppe_hits.get(key, 0) - 1)

                    if c.ppe_hits.get(key, 0) >= PPE_CONFIRM_HITS:
                        if item == 1:
                            c.hardhat_until = now + 6
                        else:
                            c.vest_until = now + 6

                        if now - c.ppe_logged.get(key, 0) >= EVENT_COOLDOWN:
                            event_name = "NO_HARDHAT" if item == 1 else "NO_VEST"
                            c.log("ppe", tid, event_name)
                            if c.event_manager:
                                c.event_manager.emit(
                                    c, event_name, 1.0, track_id=tid,
                                    details={
                                        "site_mode": c.site_mode,
                                        "inside_danger_zone": tid in inside,
                                        "confirmed_hits": c.ppe_hits[key],
                                    }
                                )
                            c.ppe_logged[key] = now
                        c.ppe_hits[key] = 0
        # 위험구역에 잠깐 스친 경우는 저장하지 않고, 연속 확인 후에만 침입 확정
        all_ids = set(c.intrusion_hits) | inside
        for tid in all_ids:
            if tid in inside:
                c.intrusion_hits[tid] = c.intrusion_hits.get(tid, 0) + 1
            else:
                c.intrusion_hits[tid] = max(0, c.intrusion_hits.get(tid, 0) - 1)

            if c.intrusion_hits.get(tid, 0) >= INTRUSION_CONFIRM_HITS:
                if now - c.danger_logged.get(tid, 0) >= EVENT_COOLDOWN:
                    c.log("danger", tid)
                    if c.event_manager:
                        c.event_manager.emit(
                            c, "INTRUSION", 1.0, track_id=tid,
                            details={"confirmed_hits": c.intrusion_hits[tid]}
                        )
                    c.danger_logged[tid] = now
                c.intrusion_hits[tid] = 0
        c.previous_danger = inside

        # CROWD: 화면 전체 또는 위험구역 내부 사람 수를 이용한 1차 구현
        if inspect:
            person_count = sum(1 for kind, _ in objects.values() if kind == 0)
            danger_count = len(inside)

            # 위험구역이 설정되어 있으면 그 안의 밀집을 우선 판단.
            # 일반 화면은 8명 이상일 때만 후보로 보아 불필요한 CROWD 경보를 줄인다.
            if danger is not None:
                crowd_now = danger_count >= CROWD_DANGER_THRESHOLD
                risk = "HIGH"
            else:
                crowd_now = person_count >= CROWD_SCREEN_THRESHOLD
                risk = "MEDIUM"

            c.crowd_hits = c.crowd_hits + 1 if crowd_now else max(0, c.crowd_hits - 1)
            if c.crowd_hits >= CROWD_CONFIRM_HITS:
                c.crowd_until = now + 6
                if c.event_manager:
                    c.event_manager.emit(
                        c, "CROWD", 1.0,
                        details={"people": person_count, "danger_people": danger_count, "risk": risk}
                    )
                c.crowd_hits = 0

        for tid in list(c.last_seen):
            if now - c.last_seen[tid] > 10:
                c.last_seen.pop(tid, None)
                c.history.pop(tid, None)
                c.upright.pop(tid, None)
                c.fall_hits.pop(tid, None)
                c.intrusion_hits.pop(tid, None)
                for item in (1, 2):
                    c.ppe_hits.pop((tid, item), None)
        c.fall_until = {tid: until for tid, until in c.fall_until.items() if now < until}
        warnings = (["DANGER! INTRUSION"] if inside else [])
        ppe_required_now = c.site_mode == "ON" or (c.site_mode == "AUTO" and danger is not None)
        warnings += (["WARNING: NO HARDHAT"] if ppe_required_now and now < c.hardhat_until else [])
        warnings += (["WARNING: NO SAFETY VEST"] if ppe_required_now and now < c.vest_until else [])
        warnings += (["FIRE DETECTED"] if now < c.fire_until else [])
        warnings += (["SMOKE DETECTED"] if now < c.smoke_until else [])
        warnings += (["CROWD DETECTED"] if now < c.crowd_until else [])
        if c.night_mode and c.show_fire_smoke:
            warnings += [f"LOW LIGHT MODE | SMOKE THR:{c.smoke_conf_threshold:.2f}"]
        warnings += [f"POSSIBLE FALL ID:{tid}" for tid in list(c.fall_until)[:3]]
        for row, text in enumerate(warnings):
            cv2.putText(image, text, (15, 30 + row * 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        mask = np.any(image != frame, axis=2)  # 원본 배경을 제외하고 감지 선·글자만 분리
        with c.lock:
            c.overlay_mask, c.overlay_time = mask, time.monotonic()
            c.raw, c.image, c.objects = frame, image, objects
            c.people = sum(int(box.cls[0]) == 0 for box in result.boxes)
            c.vehicles = sum(int(box.cls[0]) in VEHICLES for box in result.boxes)

def live_snapshot(channel):
    with channel.lock:
        raw = channel.pending if channel.pending is not None else channel.raw
        image, mask, objects = channel.image, channel.overlay_mask, channel.objects
        status = channel.status
        recent = time.monotonic() - channel.overlay_time <= 0.7

    if raw is None:
        return None, None, {}

    # 로컬 영상이 끝난 경우:
    # 마지막 AI 분석 프레임을 그대로 유지해서 나침반/경고/박스가 사라지지 않게 한다.
    if status == "Ended" and image is not None and image.shape == raw.shape:
        return raw, image.copy(), objects

    display = raw.copy()  # 실시간 스트림은 최신 수신 영상을 우선 표시
    if recent and image is not None and mask is not None and image.shape == raw.shape:
        display[mask] = image[mask]
    else:
        objects = {}  # 오래된 박스로 엉뚱한 대상을 선택하지 않음

    return raw, display, objects

def target_crop(channel, raw, objects):
    target = channel.selected_id
    if raw is None or target is None:
        return None, ""
    if target in objects:
        _, (x1, y1, x2, y2) = objects[target]
        h, w = raw.shape[:2]
        crop = raw[max(0, y1 - 60):min(h, y2 + 60), max(0, x1 - 60):min(w, x2 + 60)]
        if crop.size:
            channel.last_zoom, channel.zoom_time = crop.copy(), time.time()
            return crop, f"Selected ID:{target}"
    if channel.last_zoom is not None and time.time() - channel.zoom_time < 5:
        return channel.last_zoom, f"LOST ID:{target}"
    return None, ""

def shortcut_code(event):
    code = event.key()
    if code == 16777216:
        return 27  # Qt Escape
    if code in [ord(c) for c in "1234VSZMDRPOUQFKHGJBNAY"]:
        return code + 32 if 65 <= code <= 90 else code
    text = event.text().lower()
    if len(text) == 1 and text in "1234vszmdrpouqfkhgjbnay":
        return ord(text)
    if sys.platform == "darwin":
        physical = {18: "1", 19: "2", 20: "3", 21: "4", 9: "v", 1: "s", 6: "z", 46: "m", 2: "d", 15: "r", 35: "p", 31: "o", 32: "u", 12: "q", 3: "f", 40: "k", 4: "h", 5: "g", 38: "j", 11: "b", 45: "n", 0: "a", 16: "y", 53: "\x1b"}
        key = physical.get(event.nativeVirtualKey())
        if key is not None:
            return ord(key)  # 맥 한글 입력 상태에서도 실제 키 위치로 처리
    return None

class MainView:
    def __init__(self, dashboard):
        from PySide6.QtCore import Qt, QObject, QEvent
        from PySide6.QtGui import QImage, QPixmap
        from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy
        self.QImage, self.QPixmap = QImage, QPixmap
        owner = self
        class Canvas(QLabel):
            def mousePressEvent(self, event):
                if event.button() == Qt.LeftButton:
                    self.setFocus()
                    dashboard.mouse(cv2.EVENT_LBUTTONDOWN, int(event.position().x()), int(event.position().y()), 0, None)
            def mouseReleaseEvent(self, event):
                if event.button() == Qt.LeftButton:
                    dashboard.mouse(cv2.EVENT_LBUTTONUP, int(event.position().x()), int(event.position().y()), 0, None)
            def mouseMoveEvent(self, event):
                dashboard.mouse(cv2.EVENT_MOUSEMOVE, int(event.position().x()), int(event.position().y()), 0, None)
            def mouseDoubleClickEvent(self, event):
                if event.button() == Qt.LeftButton:
                    dashboard.mouse(cv2.EVENT_LBUTTONDBLCLK, int(event.position().x()), int(event.position().y()), 0, None)
        class MainWindow(QWidget):
            def closeEvent(self, event):
                owner.closed = True
                event.accept()
        self.closed = False
        self.window = MainWindow()
        self.window.setWindowTitle(WINDOW)
        self.label = Canvas()
        self.label.setMinimumSize(1, 1)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)  # 창을 늘려도 영상 크기가 레이아웃을 밀지 않음
        self.label.setFocusPolicy(Qt.StrongFocus)
        self.label.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.label.setMouseTracking(True)
        layout = QVBoxLayout(self.window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self.window.setMinimumSize(1280, 720)
        self.window.resize(WIDTH, HEIGHT)
        self.window.show()
        self.label.setFocus()
        class KeyFilter(QObject):
            def eventFilter(self, watched, event):
                if event.type() not in (QEvent.KeyPress, QEvent.ShortcutOverride):
                    return False
                if not hasattr(watched, "window"):
                    return False
                top = watched.window()
                zoom = dashboard.floating_zoom
                if top is not owner.window and (zoom is None or top is not zoom.window):
                    return False  # 파일·주소 입력 대화상자의 키는 가로채지 않음
                code = shortcut_code(event)
                if code is None:
                    return False
                event.accept()
                if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
                    dashboard.key_queue.append(code)
                return True  # 자식 위젯 포커스와 관계없이 한 번만 전달
        self.key_filter = KeyFilter(self.window)
        dashboard.dialog_app.installEventFilter(self.key_filter)
    def canvas_size(self):
        return max(1, self.label.width()), max(1, self.label.height())
    def toggle_fullscreen(self):
        if self.window.isFullScreen():
            self.window.showNormal()
        else:
            self.window.showFullScreen()
        self.label.setFocus()
    def is_fullscreen(self):
        return self.window.isFullScreen()
    def update(self, image):
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        h, w = rgb.shape[:2]
        qimage = self.QImage(rgb.data, w, h, rgb.strides[0], self.QImage.Format_RGB888).copy()
        self.label.setPixmap(self.QPixmap.fromImage(qimage))
    def close(self):
        self.window.close()

class FloatingZoom:
    def __init__(self, app, on_key):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPixmap
        from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy
        self.app, self.Qt, self.QImage, self.QPixmap = app, Qt, QImage, QPixmap
        self.token, self.dismissed = None, False
        owner = self
        class ZoomWindow(QWidget):
            def keyPressEvent(self, event):
                code = event.key()
                if code == Qt.Key_Escape:
                    on_key(27)
                elif code in [ord(c) for c in "1234VSZMDRPOUQFKH"]:
                    if not event.isAutoRepeat():
                        on_key(code + 32 if 65 <= code <= 90 else code)
                else:
                    super().keyPressEvent(event)
                    return
                event.accept()  # 확대창에서도 단축키 전달, 경고음 방지
            def closeEvent(self, event):
                owner.dismissed = True  # X로 닫으면 같은 영역이 자동으로 다시 열리지 않음
                event.accept()
        self.window = ZoomWindow()
        self.window.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)  # 메인 창을 눌러도 확대창이 앞에 유지
        self.window.setAttribute(Qt.WA_ShowWithoutActivating, True)  # 확대창이 메인 창의 키보드 포커스를 가져가지 않음
        self.window.resize(640, 360)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumSize(1, 1)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)  # 영상 크기가 창을 밀어내지 않도록 설정
        self.label.setStyleSheet("background: #121212;")
        layout = QVBoxLayout(self.window)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.label)
    def update(self, image, title, token):
        if image is None:
            self.window.hide()
            self.token, self.dismissed = None, False
            return
        if token != self.token:
            self.token, self.dismissed = token, False
        if self.dismissed:
            return
        self.window.setWindowTitle(title)
        if not self.window.isVisible():
            self.window.show()
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        h, w = rgb.shape[:2]
        qimage = self.QImage(rgb.data, w, h, rgb.strides[0], self.QImage.Format_RGB888).copy()
        pixmap = self.QPixmap.fromImage(qimage).scaled(self.label.size(), self.Qt.IgnoreAspectRatio, self.Qt.SmoothTransformation)
        self.label.setPixmap(pixmap)  # 처음 트래커처럼 확대 영역 전체를 창에 꽉 채움
    def close(self):
        self.window.close()

class Dashboard:
    def __init__(self, channels, analyzer, telemetry, safety, telemetry_input, agent_state, agent_engine, command_builder, command_validator, io_bridge, mission_executor):
        self.channels, self.analyzer = channels, analyzer
        self.telemetry, self.safety = telemetry, safety
        self.telemetry_input = telemetry_input
        self.agent_state = agent_state
        self.agent_engine = agent_engine
        self.command_builder = command_builder
        self.command_validator = command_validator
        self.io_bridge = io_bridge
        self.mission_executor = mission_executor
        self.selected, self.layout = 0, 1
        self.last_multi_layout = 2
        self.maps, self.tile_maps, self.drag_channel = {}, {}, None
        self.dialog_app, self.retired = None, []
        self.floating_zoom = None
        self.main_view = None
        self.show_map = False
        self.key_queue = deque()
    def ensure_qt(self):
        from PySide6.QtWidgets import QApplication
        self.dialog_app = QApplication.instance() or QApplication([])
        self.dialog_app.setQuitOnLastWindowClosed(False)
        return self.dialog_app
    def add_source(self, stream=False):
        try:
            from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog
            self.ensure_qt()
            labels = [f"Camera {i + 1}" for i in range(len(self.channels))]
            slot, ok = QInputDialog.getItem(None, "영상 위치", "영상을 넣을 화면을 선택하세요:", labels, self.selected, False)
            if not ok:
                return
            index = labels.index(slot)
            if stream:
                source, ok = QInputDialog.getText(None, "스트림 추가", "RTSP / HTTP / YouTube 주소:")
                if not ok:
                    return
                source = source.strip()
                if not source.startswith(("rtsp://", "rtsps://", "http://", "https://")):
                    print("RTSP / HTTP / YouTube 주소를 입력해주세요.")
                    return
            else:
                source, _ = QFileDialog.getOpenFileName(None, "영상 선택", str(ROOT / "videos"), "Video (*.mp4 *.mov *.avi *.mkv *.webm *.m4v);;All files (*)")
            if not source:
                return
            previous = self.channels[index]
            previous.stop()
            self.retired.append(previous)
            self.channels[index] = Channel(index + 1, source, previous.show_ppe, previous.event_manager)
            self.maps = {}
            self.tile_maps = {}
            self.select(index)
            print(f"Camera {index + 1}: 영상 변경 완료")
        except ImportError:
            print("영상 추가 창에 PySide6가 필요합니다: python -m pip install PySide6")
    def set_layout(self, layout):
        layout = int(np.clip(layout, 1, len(self.channels)))
        if layout > 1:
            self.last_multi_layout = layout
        self.layout, self.drag_channel = layout, None
        for c in self.channels:
            c.drag_start = c.drag_end = None

    def input_battery(self):
        """테스트용 배터리 입력. 실제 드론 연동 시 자동 수신으로 교체."""
        try:
            from PySide6.QtWidgets import QInputDialog
            self.ensure_qt()
            current = float(self.telemetry.snapshot()["battery"])
            percent, ok = QInputDialog.getDouble(
                None,
                "배터리 입력",
                "배터리 잔량 (%):",
                current,
                0.0,
                100.0,
                1,
            )
            if not ok:
                return

            self.telemetry_input.test_battery(percent)

            if percent <= BATTERY_RTH:
                level = "RTH"
            elif percent <= BATTERY_LIMIT:
                level = "LIMIT"
            elif percent <= BATTERY_WARN:
                level = "WARN"
            else:
                level = "OK"

            print(f"[BATTERY] TEST INPUT -> {percent:.1f}% | {level}")
        except ImportError:
            print("배터리 입력창에 PySide6가 필요합니다: python -m pip install PySide6")
        except ValueError as exc:
            print(f"[BATTERY] 입력 오류: {exc}")

    def input_altitude(self):
        """테스트용 고도 입력. 실제 PX4/ROS2 연동 시 자동 수신으로 교체."""
        try:
            from PySide6.QtWidgets import QInputDialog
            self.ensure_qt()
            telem = self.telemetry.snapshot()
            current = float(telem["altitude_m"]) if telem["altitude_m"] is not None else 30.0
            altitude, ok = QInputDialog.getDouble(
                None,
                "고도 입력",
                "고도 (m):",
                current,
                -500.0,
                10000.0,
                1,
            )
            if not ok:
                return
            self.telemetry_input.test_altitude(altitude)
            print(f"[ALT] TEST INPUT -> {altitude:.1f} m")
        except ImportError:
            print("고도 입력창에 PySide6가 필요합니다: python -m pip install PySide6")

    def input_heading(self):
        """테스트용 기체 방향 입력. 0=N, 90=E, 180=S, 270=W."""
        try:
            from PySide6.QtWidgets import QInputDialog
            self.ensure_qt()
            telem = self.telemetry.snapshot()
            current = float(telem["heading_deg"]) if telem["heading_deg"] is not None else 0.0
            heading, ok = QInputDialog.getDouble(
                None,
                "Heading 입력",
                "기체 방향 (deg, 0=N / 90=E / 180=S / 270=W):",
                current,
                0.0,
                359.9,
                1,
            )
            if not ok:
                return
            self.telemetry_input.test_heading(heading)
            snap = self.telemetry.snapshot()
            print(f"[HDG] TEST INPUT -> {snap['heading_deg']:.1f} deg")
        except ImportError:
            print("Heading 입력창에 PySide6가 필요합니다: python -m pip install PySide6")

    def input_gps(self):
        """테스트용 GPS 입력. 실제 PX4/ROS2 연동 시 자동 갱신으로 교체."""
        try:
            from PySide6.QtWidgets import QInputDialog
            self.ensure_qt()
            telem = self.telemetry.snapshot()
            default_lat = float(telem["lat"]) if telem["lat"] is not None else 37.5665
            default_lon = float(telem["lon"]) if telem["lon"] is not None else 126.9780

            lat, ok = QInputDialog.getDouble(
                None, "GPS 입력", "위도 (Latitude):",
                default_lat, -90.0, 90.0, 6
            )
            if not ok:
                return

            lon, ok = QInputDialog.getDouble(
                None, "GPS 입력", "경도 (Longitude):",
                default_lon, -180.0, 180.0, 6
            )
            if not ok:
                return

            self.telemetry_input.test_gps(lat, lon)
            snap = self.telemetry.snapshot()
            print(
                f"[GPS] CURRENT=({snap['lat']:.6f}, {snap['lon']:.6f}) | "
                f"HOME=({snap['home_lat']:.6f}, {snap['home_lon']:.6f})"
            )
        except ImportError:
            print("GPS 입력창에 PySide6가 필요합니다: python -m pip install PySide6")
        except ValueError as exc:
            print(f"[GPS] 입력 오류: {exc}")

    def set_home_here(self):
        if self.telemetry.set_home_current():
            snap = self.telemetry.snapshot()
            print(f"[GPS] HOME saved -> ({snap['home_lat']:.6f}, {snap['home_lon']:.6f})")
        else:
            print("[GPS] 먼저 GPS 좌표를 입력하세요. [G]")

    @staticmethod
    def _gps_xy(lat, lon, home_lat, home_lon):
        """HOME 기준 대략적인 동/북 방향 거리(m)."""
        lat_scale = 111320.0
        lon_scale = 111320.0 * max(0.2, abs(np.cos(np.radians(home_lat))))
        east_m = (lon - home_lon) * lon_scale
        north_m = (lat - home_lat) * lat_scale
        return east_m, north_m

    def draw_gps_map(self, canvas, telem, event_manager):
        """HOME/현재 드론/최근 이벤트를 표시하는 오프라인 로컬 GPS 미니맵."""
        if not self.show_map:
            return

        panel_w, panel_h = 300, 220
        x2 = WIDTH - 18
        y2 = HEIGHT - 45
        x1 = max(8, x2 - panel_w)
        y1 = max(HEADER + 8, y2 - panel_h)

        overlay = canvas.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.82, canvas, 0.18, 0, canvas)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (130, 130, 130), 1)
        cv2.putText(
            canvas, "GPS MAP [N]", (x1 + 10, y1 + 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 1
        )

        home_lat, home_lon = telem["home_lat"], telem["home_lon"]
        lat, lon = telem["lat"], telem["lon"]

        if home_lat is None or home_lon is None:
            cv2.putText(
                canvas, "HOME/GPS not set", (x1 + 18, y1 + 58),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1
            )
            return

        points = []
        current_xy = None
        if lat is not None and lon is not None:
            current_xy = self._gps_xy(lat, lon, home_lat, home_lon)
            points.append(current_xy)

        events = []
        if event_manager is not None:
            with event_manager.lock:
                raw_events = list(event_manager.recent_event_locations)
            for item in raw_events:
                exy = self._gps_xy(item["lat"], item["lon"], home_lat, home_lon)
                events.append((item, exy))
                points.append(exy)

        max_abs = 50.0
        if points:
            max_abs = max(
                max_abs,
                max(max(abs(px), abs(py)) for px, py in points) * 1.25
            )

        map_left, map_top = x1 + 22, y1 + 38
        map_right, map_bottom = x2 - 18, y2 - 28
        map_w = max(1, map_right - map_left)
        map_h = max(1, map_bottom - map_top)
        cx = (map_left + map_right) // 2
        cy = (map_top + map_bottom) // 2

        def to_px(east_m, north_m):
            px = int(cx + (east_m / max_abs) * (map_w * 0.45))
            py = int(cy - (north_m / max_abs) * (map_h * 0.45))
            return px, py

        cv2.line(canvas, (map_left, cy), (map_right, cy), (75, 75, 75), 1)
        cv2.line(canvas, (cx, map_top), (cx, map_bottom), (75, 75, 75), 1)
        cv2.putText(canvas, "N", (cx - 5, map_top + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (210, 210, 210), 1)
        cv2.putText(canvas, "E", (map_right - 12, cy - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (210, 210, 210), 1)

        cv2.drawMarker(
            canvas, (cx, cy), (0, 255, 255),
            markerType=cv2.MARKER_STAR, markerSize=18, thickness=2
        )
        cv2.putText(canvas, "HOME", (cx + 8, cy - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)

        for item, (east_m, north_m) in events[-10:]:
            px, py = to_px(east_m, north_m)
            cv2.circle(canvas, (px, py), 4, (0, 0, 255), -1)
            cv2.putText(
                canvas, str(item["event"])[:8], (px + 5, py - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 0, 255), 1
            )

        if current_xy is not None:
            px, py = to_px(*current_xy)
            cv2.circle(canvas, (px, py), 7, (0, 255, 0), -1)
            cv2.circle(canvas, (px, py), 11, (0, 255, 0), 1)
            cv2.putText(canvas, "DRONE", (px + 8, py + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 0), 1)

        cv2.putText(
            canvas,
            f"range +/-{int(max_abs)}m | events:{len(events)}",
            (x1 + 10, y2 - 9),
            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1
        )

    def choose_view_count(self):
        """1~16 사이의 원하는 관제 화면 수를 직접 입력한다."""
        try:
            from PySide6.QtWidgets import QInputDialog
            self.ensure_qt()
            count, ok = QInputDialog.getInt(
                None,
                "VIEW COUNT",
                f"표시할 카메라 화면 수 (1~{len(self.channels)}):",
                self.layout,
                1,
                len(self.channels),
                1,
            )
            if ok:
                self.set_layout(count)
                print(f"[VIEW] {count} camera view")
        except ImportError:
            print("VIEW COUNT 입력창에 PySide6가 필요합니다: python -m pip install PySide6")

    @staticmethod
    def grid_shape(count):
        """화면 수에 맞춰 가능한 한 균형 잡힌 행/열을 자동 계산."""
        if count <= 1:
            return 1, 1
        cols = int(np.ceil(np.sqrt(count)))
        rows = int(np.ceil(count / cols))
        return cols, rows

    def select(self, index):
        if index >= len(self.channels):
            return
        self.selected, self.drag_channel = index, None
        if self.layout != 1 and index >= self.layout:
            self.set_layout(index + 1)
    def mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y < HEADER:
            if 40 <= y < 66 and WIDTH - 145 <= x < WIDTH - 10:
                if self.main_view is not None:
                    self.main_view.toggle_fullscreen()
                return
            if 5 <= y <= 35 and 950 <= x < 1095:
                self.add_source(False)
                return
            if 5 <= y <= 35 and 1105 <= x < 1270:
                self.add_source(True)
                return
            if y < 36:
                for index, name in enumerate(TOOLS):
                    if 10 + index * 105 <= x < 110 + index * 105:
                        c = self.channels[self.selected]
                        c.drag_start = c.drag_end = None
                        if name == "RESET":
                            with c.lock:
                                c.selected_id = c.roi = c.danger = None
                                c.last_zoom = None
                            c.mode = "SELECT"
                        else:
                            c.mode = name
                        return
            for index, layout in enumerate((1, 2, 4)):
                left = 570 + index * 90
                if left <= x < left + 82 and y < 36:
                    self.set_layout(layout)
                    return
            if 840 <= x < 945 and y < 36:
                self.choose_view_count()
                return
            return

        # 다중 VIEW에서는 화면 전체 타일을 마우스로 클릭해 카메라를 선택할 수 있다.
        # 다른 카메라를 처음 클릭한 경우에는 "카메라 선택"만 수행해서
        # SELECT/DANGER/ZOOM 동작이 실수로 동시에 실행되지 않게 한다.
        if self.drag_channel is None and event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONDBLCLK):
            tile_index = next(
                (
                    i for i, rect in self.tile_maps.items()
                    if rect[0] <= x < rect[0] + rect[2]
                    and rect[1] <= y < rect[1] + rect[3]
                ),
                None,
            )
            if tile_index is not None:
                if event == cv2.EVENT_LBUTTONDBLCLK:
                    self.select(tile_index)
                    if self.layout == 1:
                        restore_layout = max(2, min(self.last_multi_layout, len(self.channels)))
                        self.set_layout(restore_layout)
                        print(f"[CAMERA] 1 VIEW -> {restore_layout} VIEW")
                    else:
                        current_layout = self.layout
                        self.last_multi_layout = current_layout
                        self.set_layout(1)
                        print(f"[CAMERA] Mouse selected Camera {tile_index + 1} -> 1 VIEW")
                    return

                if tile_index != self.selected:
                    self.select(tile_index)
                    print(f"[CAMERA] Mouse selected Camera {tile_index + 1}")
                    return

        index = self.drag_channel if self.drag_channel is not None else next((i for i, rect in self.maps.items() if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]), None)
        if index is None or index not in self.maps:
            return
        c = self.channels[index]
        mx, my, mw, mh, fw, fh = self.maps[index]
        point = (int(np.clip((x - mx) * fw / mw, 0, fw - 1)), int(np.clip((y - my) * fh / mh, 0, fh - 1)))
        if event == cv2.EVENT_LBUTTONDBLCLK:
            self.select(index)
            if self.layout == 1:
                self.set_layout(max(2, min(self.last_multi_layout, len(self.channels))))
            else:
                self.last_multi_layout = self.layout
                self.set_layout(1)
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            self.select(index)
            if not (mx <= x < mx + mw and my <= y < my + mh):
                return
            with c.lock:
                if c.mode == "SELECT":
                    objects = c.objects if time.monotonic() - c.overlay_time <= 0.7 else {}
                    c.selected_id = next((tid for tid, (_, (x1, y1, x2, y2)) in objects.items() if x1 - 10 <= point[0] <= x2 + 10 and y1 - 10 <= point[1] <= y2 + 10), None)
                    c.last_zoom = None
                elif c.mode in ("ZOOM", "DANGER"):
                    c.drag_start = c.drag_end = point
                    self.drag_channel = index
                elif c.mode == "MOVE" and c.roi is not None:
                    x1, y1, x2, y2 = c.roi
                    rw, rh = x2 - x1, y2 - y1
                    left, top = max(0, min(point[0] - rw // 2, fw - rw)), max(0, min(point[1] - rh // 2, fh - rh))
                    c.roi = left, top, left + rw, top + rh
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_channel is not None:
            c.drag_end = point
        elif event == cv2.EVENT_LBUTTONUP and self.drag_channel is not None:
            if c.drag_start is not None:
                x1, x2 = sorted((c.drag_start[0], point[0]))
                y1, y2 = sorted((c.drag_start[1], point[1]))
                if x2 - x1 > 5 and y2 - y1 > 5:
                    with c.lock:
                        if c.mode == "ZOOM":
                            c.roi, c.selected_id = (x1, y1, x2, y2), None
                        elif c.mode == "DANGER":
                            c.danger = x1, y1, x2, y2
            c.drag_start = c.drag_end = None
            self.drag_channel = None
    def zoom(self):
        c = self.channels[self.selected]
        raw, _, _ = live_snapshot(c)
        with c.lock:
            roi = c.roi
        image = None
        if raw is not None and roi is not None:
            x1, y1, x2, y2 = roi
            h, w = raw.shape[:2]
            crop = raw[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            if crop.size:
                image = crop
        if self.floating_zoom is None and image is not None:
            self.floating_zoom = FloatingZoom(self.ensure_qt(), self.key_queue.append)
        if self.floating_zoom is not None:
            self.floating_zoom.update(image, f"Camera {c.number} - Area Zoom", (id(c), roi))
    def draw(self):
        global WIDTH, HEIGHT
        if self.main_view is not None:
            WIDTH, HEIGHT = self.main_view.canvas_size()  # 실제 창 크기로 매번 배치 계산
        canvas = np.full((HEIGHT, WIDTH, 3), 22, np.uint8)
        self.zoom()
        view_left = 0  # 안내판 없이 영상이 창 전체 너비를 사용
        view_width = WIDTH - view_left
        c = self.channels[self.selected]
        self.io_bridge.poll_telemetry()
        self.telemetry.sim_heartbeat()
        self.safety.update(c)
        self.agent_state.update(self.channels)
        agent = self.agent_state.snapshot()
        decision = self.agent_engine.decide(agent)
        command = self.command_builder.build(decision, agent)
        validation = self.command_validator.validate(command, agent)
        safe_command = validation["safe_command"]
        adapter_result = self.io_bridge.send_command(safe_command)
        telem = self.telemetry.snapshot()
        mission = self.mission_executor.update(
            safe_command,
            adapter_result,
            telem,
            agent_state=agent,
            decision=decision,
            validation=validation,
        )
        cv2.rectangle(canvas, (WIDTH - 145, 40), (WIDTH - 10, 65), (95, 75, 30), -1)
        full_text = "WINDOW [F]" if self.main_view is not None and self.main_view.is_fullscreen() else "FULL [F]"
        cv2.putText(canvas, full_text, (WIDTH - 136, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, getattr(cv2, "LINE_AA", 16))
        for index, name in enumerate(TOOLS):
            left = 10 + index * 105
            color = (0, 150, 0) if c.mode == name else (65, 65, 65)
            cv2.rectangle(canvas, (left, 5), (left + 100, 35), color, -1)
            cv2.putText(canvas, name, (left + 8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        for index, layout in enumerate((1, 2, 4)):
            left = 570 + index * 90
            cv2.rectangle(canvas, (left, 5), (left + 82, 35), (120, 90, 0) if self.layout == layout else (65, 65, 65), -1)
            cv2.putText(canvas, f"{layout} VIEW", (left + 7, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)
        cv2.rectangle(canvas, (840, 5), (945, 35), (120, 90, 0) if self.layout not in (1, 2, 4) else (65, 65, 65), -1)
        cv2.putText(canvas, f"VIEW:{self.layout}", (850, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 1)
        gps_text = "GPS:--" if telem["lat"] is None or telem["lon"] is None else f"GPS:{telem['lat']:.5f},{telem['lon']:.5f}"
        home_text = "HOME:--" if telem["home_lat"] is None or telem["home_lon"] is None else f"HOME:{telem['home_lat']:.5f},{telem['home_lon']:.5f}"

        battery = telem["battery"]
        if battery <= BATTERY_RTH:
            battery_state = "RTH"
        elif battery <= BATTERY_LIMIT:
            battery_state = "LIMIT"
        elif battery <= BATTERY_WARN:
            battery_state = "WARN"
        else:
            battery_state = "OK"

        fire_state = "ON" if c.show_fire_smoke else "OFF"
        tel_state = telem["telemetry_source"] if telem["telemetry_connected"] else f"{telem['telemetry_source']}:OFF"
        profile_key = telem["drone_profile"]["key"].replace("SKYDIO_", "").replace("_REF", "")
        agent_text = "READY" if agent["ready"] else "WAIT"
        action_text = decision["primary_action"]
        active_events = agent["active_events"]
        event_text = ",".join(active_events[:3]) if active_events else "NONE"
        if len(active_events) > 3:
            event_text += f"+{len(active_events) - 3}"
        alt_text = "--" if telem["altitude_m"] is None else f"{telem['altitude_m']:.1f}m"
        if telem["heading_deg"] is None:
            hdg_text = "--"
        else:
            hdg = telem["heading_deg"] % 360.0
            hdg_names = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
            hdg_name = hdg_names[int((hdg + 22.5) // 45) % 8]
            hdg_text = f"{hdg:.0f}deg({hdg_name})"
        gps_short = "GPS:SET" if telem["lat"] is not None and telem["lon"] is not None else "GPS:--"
        home_short = "HOME:SET" if telem["home_lat"] is not None and telem["home_lon"] is not None else "HOME:--"
        cv2.putText(
            canvas,
            f"Cam:{c.number} | Tool:{c.mode} | SITE:{c.site_mode} | FIRE:{fire_state} | "
            f"BAT:{battery:.1f}%[{battery_state}] | {gps_short} | {home_short} | "
            f"ALT:{alt_text} | HDG:{hdg_text} | TEL:{tel_state} | X10:{profile_key} | {telem['flight_mode']}",
            (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 255, 255), 1
        )
        if battery_state != "OK":
            warning_text = {
                "WARN": f"BATTERY LOW: {battery:.1f}% (WARN)",
                "LIMIT": f"BATTERY LOW: {battery:.1f}% (MISSION LIMIT)",
                "RTH": f"BATTERY CRITICAL: {battery:.1f}% (RTH REQUEST)",
            }[battery_state]
            cv2.putText(
                canvas,
                warning_text,
                (WIDTH - 430, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (0, 0, 255),
                2,
            )

        # RTH 상태를 관제 화면에서 눈에 띄게 표시한다.
        flight_mode = telem["flight_mode"]
        if flight_mode in ("RTH_REQUESTED", "RETURNING_HOME", "LAND"):
            rth_text = {
                "RTH_REQUESTED": "RTH: REQUESTED",
                "RETURNING_HOME": "RTH: RETURNING HOME",
                "LAND": "RTH: LAND",
            }[flight_mode]
            cv2.putText(
                canvas,
                rth_text,
                (WIDTH - 250, 88),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 0, 255) if flight_mode != "LAND" else (0, 255, 255),
                2,
            )

        for text, left, right in (("ADD FILE [O]", 950, 1095), ("ADD URL [U]", 1105, 1270)):
            cv2.rectangle(canvas, (left, 5), (right, 35), (100, 85, 25), -1)
            cv2.putText(canvas, text, (left + 8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        visible = [self.selected] if self.layout == 1 else list(range(self.layout))
        cols, rows = self.grid_shape(self.layout)
        cell_w, cell_h = view_width // cols, (HEIGHT - HEADER - FOOTER) // rows
        self.maps = {}
        self.tile_maps = {}
        for position, index in enumerate(visible):
            left, top = view_left + (position % cols) * cell_w, HEADER + (position // cols) * cell_h
            self.tile_maps[index] = (left, top, cell_w, cell_h)
            if index >= len(self.channels):
                cv2.putText(canvas, f"Camera {index + 1}: No source", (left + 20, top + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 1)
                continue
            channel = self.channels[index]
            raw, image, objects = live_snapshot(channel)
            with channel.lock:
                danger, roi, selected = channel.danger, channel.roi, channel.selected_id
                receiving = time.monotonic() - channel.received_at < 2
                receive_fps = channel.receive_fps if receiving else 0.0
                age = time.monotonic() - channel.overlay_time
                ai_fps = channel.fps if age < 2 else 0.0
                people, vehicles = (channel.people, channel.vehicles) if objects else (0, 0)
                stats = f"Cam {channel.number} | P:{people} V:{vehicles} | RX:{receive_fps:.1f} AI:{ai_fps:.1f} | {channel.status}"
            if image is None:
                image = np.full((360, 640, 3), 25, np.uint8)
                cv2.putText(image, f"Camera {channel.number}: {channel.status}", (20, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1)
            else:
                image = image.copy()
                # DANGER 영역은 계속 표시하지만,
                # ZOOM ROI 박스는 선택 완료 후 화면에 계속 남기지 않는다.
                # 확대 기능은 floating zoom 창에서 그대로 유지된다.
                if danger is not None:
                    cv2.rectangle(image, danger[:2], danger[2:], (0, 0, 255), 1)
                if selected in objects:
                    rect = objects[selected][1]
                    cv2.rectangle(image, rect[:2], rect[2:], (0, 255, 255), 1)
                if channel.drag_start is not None and channel.drag_end is not None:
                    color = (0, 0, 255) if channel.mode == "DANGER" else (255, 255, 0)
                    cv2.rectangle(image, channel.drag_start, channel.drag_end, color, 1)
            tile, (ox, oy, nw, nh) = fit_image(image, cell_w, cell_h - 28)
            crop, caption = target_crop(channel, raw, objects)
            if crop is not None and nw > 32 and nh > 32:
                inset_scale = min(1.0, (nw - 16) / 320, (nh - 16) / 240)
                inset_w, inset_h = max(1, round(320 * inset_scale)), max(1, round(240 * inset_scale))
                inset = cv2.resize(crop, (inset_w, inset_h), interpolation=cv2.INTER_CUBIC)  # 원래 320×240 미리보기 방식
                blurred = cv2.GaussianBlur(inset, (0, 0), 1.0)
                inset = cv2.addWeighted(inset, 1.5, blurred, -0.5, 0)
                cv2.putText(inset, caption, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
                ix, iy = ox + nw - inset_w - 8, oy + nh - inset_h - 8
                tile[iy:iy + inset_h, ix:ix + inset_w] = inset
                cv2.rectangle(tile, (ix, iy), (ix + inset_w - 1, iy + inset_h - 1), (0, 255, 255), 2)
            canvas[top:top + cell_h - 28, left:left + cell_w] = tile
            if raw is not None:
                fh, fw = raw.shape[:2]
                self.maps[index] = left + ox, top + oy, nw, nh, fw, fh
            cv2.putText(canvas, stats, (left + 6, top + cell_h - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (235, 235, 235), 1)
            cv2.rectangle(canvas, (left + 1, top + 1), (left + cell_w - 2, top + cell_h - 2), (0, 255, 180) if index == self.selected else (80, 80, 80), 2)
        # 긴 Agent/Telemetry 정보는 영상과 겹치지 않도록 전용 하단 상태바에 표시한다.
        footer_top = HEIGHT - FOOTER
        cv2.rectangle(canvas, (0, footer_top), (WIDTH, HEIGHT), (18, 18, 18), -1)
        cv2.line(canvas, (0, footer_top), (WIDTH, footer_top), (80, 80, 80), 1)

        cmd_params = safe_command.get("params", {})
        cmd_name = safe_command.get("command", "PATROL")
        if cmd_name == "ORBIT":
            cmd_detail = f"R:{cmd_params.get('radius_m', '--')}m S:{cmd_params.get('speed_mps', '--')}m/s"
        elif cmd_name == "TRACK":
            cmd_detail = f"D:{cmd_params.get('follow_distance_m', '--')}m S:{cmd_params.get('speed_mps', '--')}m/s"
        elif cmd_name == "RTH":
            cmd_detail = f"S:{cmd_params.get('speed_mps', '--')}m/s"
        elif cmd_name == "LAND":
            cmd_detail = f"DESC:{cmd_params.get('descent_speed_mps', '--')}m/s"
        elif cmd_name == "PATROL":
            cmd_detail = f"ALT:{cmd_params.get('altitude_m', '--')}m S:{cmd_params.get('speed_mps', '--')}m/s"
        elif cmd_name == "HOLD":
            cmd_detail = "SAFE HOLD"
        else:
            cmd_detail = "NO-MOTION"

        safety_flag = "OK" if validation["approved"] else "BLOCK"
        io_state = self.io_bridge.snapshot()
        adapter_flag = f"{adapter_result['adapter']}:{adapter_result['status']}"
        io_flag = f"IN:{io_state['input_source']}/OUT:{io_state['output_adapter']}"
        mission_flag = f"{mission['mission']}:{mission['status']}:{mission['progress']}%"
        agent_line = (
            f"AGENT:{agent_text} | EVT:{event_text} | ACT:{action_text} | "
            f"CMD:{cmd_name}({cmd_detail}) | SAFE:{safety_flag}/{validation['reason']} | "
            f"ADAPTER:{adapter_flag} | IO:{io_flag}"
        )
        mission_id_short = str(mission.get("mission_id") or "--")[-14:]
        telemetry_line = (
            f"MISSION:{mission_flag} | ID:{mission_id_short} | {gps_text} | {home_text} | "
            f"ALT:{alt_text} | HDG:{hdg_text} | BAT:{battery:.1f}%[{battery_state}] | "
            f"TEL:{tel_state} | DRONE:{profile_key} | FLIGHT:{telem['flight_mode']}"
        )

        cv2.putText(
            canvas, agent_line, (10, footer_top + 21),
            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1,
            getattr(cv2, "LINE_AA", 16)
        )
        cv2.putText(
            canvas, telemetry_line, (10, footer_top + 43),
            cv2.FONT_HERSHEY_SIMPLEX, 0.39, (220, 220, 220), 1,
            getattr(cv2, "LINE_AA", 16)
        )

        if self.analyzer.error:
            cv2.putText(
                canvas,
                "AI ERROR - See terminal",
                (WIDTH - 210, footer_top + 43),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.40,
                (0, 0, 255),
                1,
            )

        event_manager = c.event_manager if c is not None else None
        self.draw_gps_map(canvas, telem, event_manager)

        if self.main_view is None:
            self.ensure_qt()
            self.main_view = MainView(self)
        self.main_view.update(canvas)
        if self.dialog_app is not None:
            self.dialog_app.processEvents()  # 별도 확대창 이동·크기 조절·닫기 처리

def main():
    parser = argparse.ArgumentParser(description="드론 안전 관제 트래커: 최대 16개 카메라 슬롯")
    parser.add_argument("sources", nargs="*", help="영상 파일, RTSP/HTTP/YouTube 주소 또는 카메라 번호 (최대 16개)")
    parser.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--no-ppe", action="store_true", help="PPE 검사 끄기 (원거리 영상용, P키로 개별 전환)")
    parser.add_argument("--battery", type=float, default=100.0, help="PX4 연동 전 테스트용 배터리 퍼센트")
    parser.add_argument("--lat", type=float, default=None, help="PX4 연동 전 테스트용 현재 위도")
    parser.add_argument("--lon", type=float, default=None, help="PX4 연동 전 테스트용 현재 경도")
    parser.add_argument("--home-lat", type=float, default=None, help="테스트용 홈 위도")
    parser.add_argument("--home-lon", type=float, default=None, help="테스트용 홈 경도")
    parser.add_argument(
        "--drone-profile",
        choices=sorted(DRONE_PROFILES.keys()),
        default="SKYDIO_X10_REF",
        help="Agent/관제 기준 기체 성능 프로파일",
    )
    parser.add_argument(
        "--flight-adapter",
        choices=("SIM", "PX4", "ROS2", "SKYDIO"),
        default="SIM",
        help="비행 명령 출력 어댑터. 실제 기체 미연결 시 SIM 사용",
    )
    args = parser.parse_args()
    sources = args.sources or ["https://www.youtube.com/watch?v=C3hW1VrwmNc"]
    if not 1 <= len(sources) <= MAX_CAMERAS:
        parser.error(f"영상은 1~{MAX_CAMERAS}개까지 지정해주세요.")
    needed = ["yolo11s.pt", "yolo11n-pose.pt", "ppe_best.pt"]
    for name in needed:
        if not (ROOT / "models" / name).is_file():
            parser.error(f"모델 파일 없음: {ROOT / 'models' / name}")
    drone_profile = DRONE_PROFILES[args.drone_profile]
    telemetry = TelemetryState(
        args.battery,
        args.lat,
        args.lon,
        args.home_lat,
        args.home_lon,
        drone_profile=drone_profile,
    )
    telemetry_input = TelemetryInputAdapter(telemetry)
    print(
        f"[DRONE PROFILE] {drone_profile.display_name} | "
        f"MAX {drone_profile.max_horizontal_speed_mps:.0f}m/s | "
        f"OA {drone_profile.obstacle_avoid_speed_mps:.0f}m/s | "
        f"ASC {drone_profile.max_ascent_speed_mps:.0f}m/s | "
        f"DESC {drone_profile.max_descent_speed_mps:.0f}m/s | "
        f"FLIGHT {drone_profile.max_flight_time_min:.0f}min"
    )
    event_manager = EventManager(telemetry)
    print(
        f"[POLICY] context filter ON | detector conf=0.35 | "
        f"event cooldown={EVENT_COOLDOWN}s | retention={EVENT_RETENTION_DAYS}d | "
        f"max events/camera={MAX_EVENT_BUNDLES_PER_CAMERA}"
    )
    channels = [
        Channel(i + 1, sources[i] if i < len(sources) else "", not args.no_ppe, event_manager)
        for i in range(MAX_CAMERAS)
    ]
    analyzer = Analyzer(channels, args.device)
    safety = SafetyController(telemetry, event_manager)
    agent_state = AgentState(telemetry)
    agent_engine = AgentDecisionEngine()
    command_builder = DroneCommandBuilder()
    command_validator = CommandSafetyValidator()
    flight_adapter = create_flight_adapter(args.flight_adapter)
    io_bridge = DroneIOBridge(telemetry_input, flight_adapter)
    mission_executor = MissionExecutor()
    dashboard = Dashboard(
        channels,
        analyzer,
        telemetry,
        safety,
        telemetry_input,
        agent_state,
        agent_engine,
        command_builder,
        command_validator,
        io_bridge,
        mission_executor,
    )
    print("[AGENT STATE] Vision + Telemetry integration -> READY")
    print("[AGENT ENGINE] Decision engine -> READY")
    print("[COMMAND BUILDER] Flight command generation -> READY")
    print("[SAFETY VALIDATOR] Command safety validation -> READY")
    if args.flight_adapter == "SIM":
        print("[FLIGHT ADAPTER] SIM adapter -> READY (ACK only / no real drone control)")
    else:
        print(
            f"[FLIGHT ADAPTER] {args.flight_adapter} adapter -> STUB "
            "(interface ready / real connection not implemented)"
        )
    print(
        f"[DRONE IO] INPUT={io_bridge.input_source} | "
        f"OUTPUT={getattr(flight_adapter, 'name', 'UNKNOWN')} | bridge=READY"
    )
    print("[MISSION EXECUTOR] Mission lifecycle + JSON logging -> READY")
    print(f"[MISSION LOG] {ROOT / 'data' / 'mission_logs'}")
    print(f"[MISSION REPORT] {ROOT / 'reports' / 'mission_reports'}")
    print("[SIM HEARTBEAT] Telemetry freshness auto-refresh -> READY")
    analyzer.start()
    dashboard.ensure_qt()
    dashboard.main_view = MainView(dashboard)  # 메인·확대 창 모두 Qt로 키 입력 통일
    try:
        while True:
            dashboard.draw()
            key = dashboard.key_queue.popleft() if dashboard.key_queue else -1
            if 65 <= key <= 90:
                key += 32  # 대문자 입력도 동일한 단축키로 처리
            if key == 27 and dashboard.main_view.is_fullscreen():
                dashboard.main_view.toggle_fullscreen()
                continue
            if key in (ord("q"), 27):
                break
            if key == ord("f"):
                dashboard.main_view.toggle_fullscreen()
            if ord("1") <= key <= ord("4"):
                dashboard.select(key - ord("1"))
            elif key == ord("v"):
                next_layout = {1: 2, 2: 4, 4: 1}.get(dashboard.layout, 1)
                dashboard.set_layout(next_layout)
            elif key in map(ord, "szmdr"):
                name = dict(zip(map(ord, "szmdr"), ("SELECT", "ZOOM", "MOVE", "DANGER", "RESET")))[key]
                dashboard.mouse(cv2.EVENT_LBUTTONDOWN, 15 + TOOLS.index(name) * 105, 10, 0, None)
            elif key == ord("o"):
                dashboard.add_source(False)
            elif key == ord("u"):
                dashboard.add_source(True)
            elif key == ord("g"):
                dashboard.input_gps()
            elif key == ord("j"):
                dashboard.set_home_here()
            elif key == ord("b"):
                dashboard.input_battery()
            elif key == ord("n"):
                dashboard.show_map = not dashboard.show_map
                print(f"[MAP] -> {'ON' if dashboard.show_map else 'OFF'}")
            elif key == ord("a"):
                dashboard.input_altitude()
            elif key == ord("y"):
                dashboard.input_heading()
            elif key == ord("p"):
                c = channels[dashboard.selected]
                c.show_ppe = not c.show_ppe
                if not c.show_ppe:
                    c.hardhat_until = c.vest_until = 0
            elif key == ord("k"):
                c = channels[dashboard.selected]
                c.site_mode = {"AUTO": "ON", "ON": "OFF", "OFF": "AUTO"}[c.site_mode]
                if c.site_mode == "OFF":
                    c.hardhat_until = c.vest_until = 0
                print(f"[SITE] Camera {c.number} PPE context -> {c.site_mode}")
            elif key == ord("h"):
                c = channels[dashboard.selected]
                c.show_fire_smoke = not c.show_fire_smoke
                if not c.show_fire_smoke:
                    c.fire_result = None
                    c.fire_hits = 0
                    c.smoke_hits = 0
                    c.fire_until = 0.0
                    c.smoke_until = 0.0
                    c.smoke_centers.clear()
                    c.smoke_direction_history.clear()
                    c.smoke_direction = "UNKNOWN"
                    c.smoke_motion = 0.0
                    c.smoke_anchor = None
                    c.fire_anchor = None
                    c.fire_anchor_until = 0.0
                print(
                    f"[FIRE/SMOKE] Camera {c.number} -> "
                    f"{'ON' if c.show_fire_smoke else 'OFF'}"
                )
            if dashboard.main_view.closed:
                break
            time.sleep(0.015)  # Qt 이벤트는 draw에서 처리, OpenCV waitKey 사용하지 않음
    finally:
        analyzer.stop_event.set()
        for c in channels + dashboard.retired:
            c.stop()
        analyzer.join()  # MPS 추론 종료 후 프로그램 종료
        for c in channels + dashboard.retired:
            if c.reader is not None:
                c.reader.join(timeout=0.2)
        if dashboard.floating_zoom is not None:
            dashboard.floating_zoom.close()
        if dashboard.main_view is not None:
            dashboard.main_view.close()

if __name__ == "__main__":
    main()
