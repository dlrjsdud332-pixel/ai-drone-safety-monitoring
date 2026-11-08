from collections import deque, Counter
import argparse
import csv
import hashlib
import json
import shutil
import socket
import os
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
MODEL_ROOT = ROOT / "models"
LOW_SCORE_TRACKER_CONFIG = HERE.parent / "tools/ai/bytetrack_v02_low_score.yaml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.ai.autonomy_v02 import AutoRoiPreview, AutoSafetyController, Candidate, RateMeter, fire_roi_retry
from tools.ai.label_layout import DetectionLabelLayout
from tools.ai.smoke_motion import SmokeMotion, draw_compass, status_line as smoke_status_line
from tools.ai.hazard_fusion import HazardFusion
from tools.ai.fire_persistence import FirePersistence, SceneMotionGuard
from tools.gazebo.px4_position import Px4Position, position_packet

WINDOW = "Drone Tracking"
WIDTH, HEIGHT, HEADER, FOOTER = 1440, 810, 70, 75
TOOLS = ("SELECT", "ZOOM", "MOVE", "RESET", "DANGER")
VEHICLES = (2, 3, 5, 7)
MAX_CAMERAS = 16


def draw_top_menu_text(canvas, text, origin, scale):
    """Render only top-menu labels between OpenCV's integer stroke widths 1 and 2."""
    font = cv2.FONT_HERSHEY_DUPLEX
    (width, height), baseline = cv2.getTextSize(text, font, scale, 2)
    x1 = max(0, min(canvas.shape[1], origin[0] - 3))
    y1 = max(0, min(canvas.shape[0], origin[1] - height - 3))
    x2 = min(canvas.shape[1], origin[0] + width + 3)
    y2 = min(canvas.shape[0], origin[1] + baseline + 3)
    if x1 >= x2 or y1 >= y2:
        return
    region = canvas[y1:y2, x1:x2]
    thin = np.zeros(region.shape[:2], dtype=np.uint8)
    thick = np.zeros_like(thin)
    position = (origin[0] - x1, origin[1] - y1)
    cv2.putText(thin, text, position, font, scale, 255, 1, cv2.LINE_AA)
    cv2.putText(thick, text, position, font, scale, 255, 2, cv2.LINE_AA)
    opacity = cv2.addWeighted(thin, 0.55, thick, 0.45, 0).astype(np.float32) / 255.0
    region[:] = np.rint(region * (1 - opacity[..., None]) + 255 * opacity[..., None]).astype(np.uint8)

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
        self.battery_received_at = None
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
        self.armed = None
        self.px4_position = Px4Position()
        self.mavlink_observer = None
        # PX4 MAVLink mission progress, separate from the Agent's simulated missions.
        self.px4_mission_seq = None
        self.px4_mission_total = None
        self.px4_mission_reached = None
        self.px4_mission_state = "UNKNOWN"
        # MAVLink mission-item completion is independent of RTL/landing.
        self.px4_mission_items_complete = False
        self.px4_mission_updated_at = None
        self.telemetry_source = "SIM"
        self.telemetry_connected = True
        # Do not raise a link-loss alarm before receiving the first real PX4/UDP packet.
        self.telemetry_ever_connected = False
        self.telemetry_last_update = time.monotonic()
        self.last_received_command = None
        self.last_command_id = None

    def sim_heartbeat(self):
        """SIM 모드에서는 수동 입력이 없어도 텔레메트리가 살아있는 것으로 유지."""
        with self.lock:
            if self.telemetry_source == "SIM":
                self.telemetry_connected = True
                self.telemetry_last_update = time.monotonic()

    def snapshot(self):
        with self.lock:
            observation = (self.mavlink_observer.snapshot(time.monotonic())
                           if self.telemetry_source == 'PX4' and self.mavlink_observer is not None else {})
            connected = self.telemetry_connected and observation.get('px4_heartbeat_fresh', True)
            position = (self.px4_position.snapshot(
                time.monotonic(), connected and
                time.monotonic()-self.telemetry_last_update <= 5.)
                if self.telemetry_source == 'PX4' else {})
            return {
                "battery": round(self.battery, 1),
                "battery_valid": (self.telemetry_source != "PX4" or
                                  (self.battery_received_at is not None and
                                   time.monotonic()-self.battery_received_at <= 15.0 and
                                   connected)),
                "battery_age_s": (max(0., time.monotonic()-self.battery_received_at)
                                  if self.battery_received_at is not None else None),
                "lat": self.lat,
                "lon": self.lon,
                "home_lat": self.home_lat,
                "home_lon": self.home_lon,
                **position,
                "flight_mode": self.flight_mode,
                "rth_requested": self.rth_requested,
                "altitude_m": self.altitude_m,
                "heading_deg": self.heading_deg,
                "armed": self.armed,
                "px4_mission_seq": self.px4_mission_seq,
                "px4_mission_total": self.px4_mission_total,
                "px4_mission_reached": self.px4_mission_reached,
                "px4_mission_state": self.px4_mission_state,
                "px4_mission_items_complete": self.px4_mission_items_complete,
                "px4_mission_age_s": (round(max(0., time.monotonic() - self.px4_mission_updated_at), 1)
                                     if self.px4_mission_updated_at is not None else None),
                "telemetry_source": self.telemetry_source,
                "telemetry_connected": connected,
                "telemetry_ever_connected": self.telemetry_ever_connected,
                "telemetry_age_s": round(max(0.0, time.monotonic() - self.telemetry_last_update), 1),
                "last_received_command": self.last_received_command,
                "last_command_id": self.last_command_id,
                "drone_profile": self.drone_profile.snapshot(),
                **observation,
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
            if self.telemetry_source == 'PX4':
                return False  # HOME_POSITION is the sole PX4 home source.
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
            if source == 'PX4':
                self.telemetry.px4_position.ingest(packet, time.monotonic())
            if "battery" in packet and packet["battery"] is not None:
                battery = float(packet["battery"])
                if 0.0 <= battery <= 100.0:
                    self.telemetry.battery = battery
                    self.telemetry.battery_received_at = time.monotonic()

            if packet.get("lat") is not None and packet.get("lon") is not None:
                lat = float(packet["lat"])
                lon = float(packet["lon"])
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    self.telemetry.lat = lat
                    self.telemetry.lon = lon
                    if source != "PX4" and (self.telemetry.home_lat is None or self.telemetry.home_lon is None):
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
            if packet.get("armed") is not None:
                self.telemetry.armed = bool(packet["armed"])

            for key in ("px4_mission_seq", "px4_mission_total", "px4_mission_reached"):
                if key in packet and packet[key] is not None:
                    value = int(packet[key])
                    if value >= 0:
                        setattr(self.telemetry, key, value)
                        self.telemetry.px4_mission_updated_at = time.monotonic()
            if packet.get("px4_mission_reset"):
                self.telemetry.px4_mission_seq = None
                self.telemetry.px4_mission_reached = None
                self.telemetry.px4_mission_total = None
                self.telemetry.px4_mission_items_complete = False
                self.telemetry.px4_mission_state = "UNKNOWN"
                self.telemetry.px4_mission_updated_at = time.monotonic()
            if "px4_mission_items_complete" in packet:
                self.telemetry.px4_mission_items_complete = bool(packet["px4_mission_items_complete"])
                self.telemetry.px4_mission_updated_at = time.monotonic()
            if "px4_mission_state" in packet:
                self.telemetry.px4_mission_state = str(packet["px4_mission_state"])
                self.telemetry.px4_mission_updated_at = time.monotonic()
            self.telemetry.telemetry_source = str(source)
            self.telemetry.telemetry_connected = True
            if str(source).upper() in ("PX4", "UDP"):
                self.telemetry.telemetry_ever_connected = True
            self.telemetry.telemetry_last_update = time.monotonic()
            if "last_command" in packet:
                self.telemetry.last_received_command = str(packet["last_command"])
            if "last_command_id" in packet:
                self.telemetry.last_command_id = packet["last_command_id"]

    def disconnect(self, source=None):
        with self.telemetry.lock:
            if source == 'PX4' and self.telemetry.telemetry_connected:
                self.telemetry.px4_position = Px4Position()
                self.telemetry.battery_received_at = None
                self.telemetry.px4_mission_seq = None
                self.telemetry.px4_mission_total = None
                self.telemetry.px4_mission_reached = None
                self.telemetry.px4_mission_items_complete = False
                self.telemetry.px4_mission_state = "UNKNOWN"
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
        self.safety_evidence = None
        import os
        if os.environ.get("SAFETY_EVIDENCE_DIR"):
            from tools.ai.safety_evidence import EvidenceJournal
            self.safety_evidence = EvidenceJournal(os.environ["SAFETY_EVIDENCE_DIR"])
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

    def emit(self, channel, kind, confidence=1.0, track_id=None, details=None, frame=None):
        if hasattr(channel, 'analysis_is_current') and not channel.analysis_is_current():return False
        with channel.lock:
            if hasattr(channel, 'analysis_is_current') and not channel.analysis_is_current():return False
            generation = getattr(channel, 'source_generation', 0)
            video_source = channel.source
            frame = frame.copy() if frame is not None else (channel.pending.copy() if channel.pending is not None else (channel.raw.copy() if channel.raw is not None else None))
            pre_frames = [item[1].copy() for item in list(channel.event_buffer)]
        now = time.time()
        key = (channel.number, id(channel), video_source, generation, kind, track_id)
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

        if frame is not None:
            cv2.imwrite(str(snapshot_path), frame)

        save_video = kind in VIDEO_EVENTS
        event_risk = RiskAlertEngine.NAMES[RiskAlertEngine.EVENT_SEVERITY.get(kind, 0)]
        meta = {
            "event_id": event_id,
            "event": kind,
            "risk_level": event_risk,
            "risk_policy": "RULE_BASED_PROTOTYPE",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "camera": channel.number,
            "video_source": video_source,
            "source_generation": generation,
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

        if self.safety_evidence is not None:
            self.safety_evidence.confirmed(dict(meta, snapshot_available=snapshot_path.is_file()))
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
                args=(channel, pre_frames, clip_path, generation),
                daemon=True,
            ).start()

        # 이벤트가 생길 때마다 용량 제한도 다시 확인
        self.cleanup_storage()
        print(f"[EVENT] {kind} | Cam {channel.number} | GPS=({meta['telemetry']['lat']}, {meta['telemetry']['lon']}) | {meta_path}")
        return True

    def _save_clip(self, channel, pre_frames, clip_path, generation=None):
        if generation is None:generation = getattr(channel, "source_generation", 0)
        frames = list(pre_frames)
        deadline = time.monotonic() + EVENT_POST_SECONDS
        next_sample = 0.0
        while time.monotonic() < deadline and not channel.stop_event.is_set():
            now = time.monotonic()
            if now >= next_sample:
                with channel.lock:
                    if generation != getattr(channel, "source_generation", 0):break
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
        now = time.time()  # Analyzer stores event expiry times using the wall clock.
        active_events = set()
        camera_states = []

        for c in channels:
            # 영상이 없는 슬롯도 구조에는 남기되 active 판단에서는 제외.
            has_source = (bool(c.source) and getattr(c,'status','Playing')=='Playing'
                          and getattr(c,'analysis_generation',None)==getattr(c,'source_generation',None)
                          and not (getattr(c,'stop_event',None) is not None and c.stop_event.is_set()))
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
                    "video_source": c.source,
                    "source_generation": getattr(c,"source_generation",0),
                    "analysis_frame_sequence": getattr(c,"processing_sequence",None),
                    "analysis_generation": getattr(c,"processing_generation",None),
                    "observation_received_monotonic": getattr(c,"processing_received_at",None),
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


class RiskAlertEngine:
    """Advisory-only risk evaluation and throttled local alert journal."""
    # These are configurable prototype policy values, not calibrated safety scores.
    EVENT_SEVERITY = {
        "FIRE": 3, "FALL": 3, "SMOKE": 2, "INTRUSION": 2,
        "CROWD": 2, "NO_HARDHAT": 1, "NO_VEST": 1,
    }
    NAMES = {0: "NONE", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}
    ALERT_REPEAT_SECONDS = 60

    def __init__(self):
        self.last_signature = None
        self.last_alert_at = 0.0
        self.current = {"level": "UNKNOWN", "reasons": [], "advice": "NO_CURRENT_EVIDENCE", "source": "RULE_BASED", "risk_resolved": False}

    def evaluate(self, agent_state):
        drone = agent_state.get("drone", {})
        events = set(agent_state.get("active_events", []))
        reasons = set(events)
        severity = max((self.EVENT_SEVERITY.get(event, 0) for event in events), default=0)
        source = str(drone.get("telemetry_source", "SIM")).upper()
        connected = bool(drone.get("telemetry_connected", False))
        age = float(drone.get("telemetry_age_s", 999))
        if (source in ("PX4", "UDP")
                and bool(drone.get("telemetry_ever_connected", False))
                and (not connected or age > 5.0)):
            # CONNECTING is not a loss; only an established link can be LOST.
            reasons.add("TELEMETRY_LOST")
            severity = max(severity, 3)
        if connected and source in ("PX4", "UDP", "SIM"):
            battery = float(drone.get("battery", 100))
            if battery <= BATTERY_RTH:
                reasons.add("BATTERY_CRITICAL")
                severity = max(severity, 3)
            elif battery <= BATTERY_LIMIT:
                reasons.add("BATTERY_LIMIT")
                severity = max(severity, 2)
            elif battery <= BATTERY_WARN:
                reasons.add("BATTERY_WARN")
                severity = max(severity, 1)
        priority = ["TELEMETRY_LOST", "BATTERY_CRITICAL", "FIRE", "FALL",
                    "BATTERY_LIMIT", "SMOKE", "INTRUSION", "CROWD", "BATTERY_WARN",
                    "NO_HARDHAT", "NO_VEST"]
        ordered = [name for name in priority if name in reasons] + sorted(reasons.difference(priority))
        advice = ("CHECK_LINK" if "TELEMETRY_LOST" in reasons else
                  "CHECK_BATTERY_RTH" if "BATTERY_CRITICAL" in reasons else
                  "EMERGENCY_REVIEW" if severity == 3 else
                  "OPERATOR_REVIEW" if severity == 2 else
                  "CHECK_EVENT" if severity == 1 else "NO_CURRENT_EVIDENCE")
        record = {"level": self.NAMES[severity] if severity else "UNKNOWN", "reasons": ordered,
                  "risk_resolved": False,
                  "advice": advice, "source": "RULE_BASED", "flight_control": "UNCHANGED"}
        signature = (record["level"], tuple(record["reasons"]))
        now = time.monotonic()
        lost_observation = (not severity and self.last_signature is not None
                            and signature != self.last_signature and bool(self.last_signature[1]))
        if (severity and (signature != self.last_signature or now - self.last_alert_at >= self.ALERT_REPEAT_SECONDS)) or lost_observation:
            entry = dict(record, timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),logged_at=time.time(),
                         camera_observations=agent_state.get("cameras", []),
                         flight_mode=drone.get("flight_mode"), armed=drone.get("armed"),
                         telemetry_source=source, camera_events={str(c.get("camera")): c.get("events", [])
                             for c in agent_state.get("cameras", []) if c.get("events")})
            path = ROOT / "data" / "alerts" / time.strftime("%Y-%m-%d") / "alerts.jsonl"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                print(f"[RISK ALERT] {record['level']} | {','.join(ordered)} | {advice} | {path}")
            except OSError as exc:
                print(f"[RISK ALERT] log write failed: {exc}")
            self.last_alert_at = now
        self.last_signature = signature
        self.current = record
        return record


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

        # 착륙 후에는 배터리가 낮아도 새 비행 명령을 만들지 않는다.
        if flight_mode == "LANDED":
            decision = {
                "primary_action": "HOLD", "actions": ["HOLD"],
                "reason": "ALREADY_LANDED", "priority": 100,
            }
        elif flight_mode == "LAND":
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

        # PX4 SITL is an observer only. The flight controller's actual mode takes
        # precedence over proposed Agent navigation actions. Detection events still
        # produce alerts/recording suggestions; they cannot preempt an RTL/landing.
        if str(drone.get("telemetry_source", "")).upper() == "PX4":
            px_mode = flight_mode.upper()
            px_fresh = (bool(drone.get("telemetry_connected"))
                        and float(drone.get("telemetry_age_s", 999)) <= 5.0)
            px_armed = drone.get("armed")
            # Keep detection-related non-motion actions, including FIRE/FALL alerts.
            passive_actions = [a for a in decision.get("actions", [])
                               if a in ("ALERT", "RECORD")]
            if not px_fresh:
                monitor_action, monitor_reason = "MONITOR", "PX4_TELEMETRY_STALE"
            elif px_armed is False:
                # Disarmed on the ground. Do not claim a new mission is underway.
                monitor_action, monitor_reason = "STANDBY", "PX4_DISARMED"
            elif px_mode in ("RTL", "RETURN", "AUTO.RTL", "RETURNING_HOME"):
                monitor_action, monitor_reason = "MONITOR_RTH", "PX4_RETURN_IN_PROGRESS"
            elif px_mode in ("LAND", "AUTO.LAND", "LANDING"):
                monitor_action, monitor_reason = "MONITOR_LANDING", "PX4_LANDING_IN_PROGRESS"
            else:
                monitor_action, monitor_reason = "MONITOR", f"PX4_MODE_{px_mode}"
            decision = {
                "primary_action": monitor_action,
                "actions": list(dict.fromkeys(passive_actions + [monitor_action])),
                "reason": monitor_reason,
                "priority": max(int(decision.get("priority", 0)), 95),
                "detected_event_action": decision.get("primary_action"),
                "px4_flight_mode": px_mode,
                "read_only": True,
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

        if action in {"STANDBY", "MONITOR", "MONITOR_RTH", "MONITOR_LANDING"}:
            command["motion"] = False
            command["params"] = {
                "monitor_only": True,
                "actual_flight_mode": decision.get("px4_flight_mode"),
                "event_recommendation": decision.get("detected_event_action"),
            }

        elif action == "PATROL":
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

        # 착륙 완료 상태에서는 저배터리 RTH 재요청도 차단한다.
        if flight_mode == "LANDED":
            if cmd != "HOLD":
                adjustments.append(f"{cmd}->HOLD")
            safe["command"] = "HOLD"
            safe["motion"] = False
            safe["params"] = {"hold_position": True}
            reason = "ALREADY_LANDED"

        # UDP 연결 끊김에서는 비행 명령을 새로 보내지 않는다.
        # 실제 기체 fail-safe는 별도 드론 컨트롤러에서 구현해야 한다.
        elif str(drone.get("telemetry_source", "SIM")) == "UDP" and (not connected or telemetry_age > 5.0):
            approved = False
            safe["command"] = "HOLD"
            safe["motion"] = False
            safe["params"] = {"hold_position": True}
            adjustments.append(f"{cmd}->HOLD")
            reason = "UDP_TELEMETRY_LOST"

        # 1) 이미 LAND 단계면 어떤 이동 명령보다 LAND가 우선.
        elif flight_mode == "LAND":
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

        # Final safety gate: a low-battery override above must never skip HOME validation.
        if safe.get("command") == "RTH" and (
            drone.get("home_lat") is None or drone.get("home_lon") is None
        ):
            approved = False
            safe["command"] = "HOLD"
            safe["motion"] = False
            safe["params"] = {"hold_position": True, "alert": "HOME_NOT_SET"}
            adjustments.append("RTH->HOLD")
            reason = "HOME_NOT_SET"

        if (drone.get('telemetry_source') == 'PX4'
                and safe.get('command') in {'PATROL','TRACK','ORBIT','RTH'}
                and not (drone.get('gps_valid') and drone.get('home_valid'))):
            approved = False
            safe['command'], safe['motion'] = 'HOLD', False
            safe['params'] = {'hold_position': True, 'alert': 'POSITION_UNVERIFIED'}
            adjustments.append(f'{cmd}->HOLD')
            reason = 'POSITION_UNVERIFIED'

        if (drone.get("telemetry_source") == "PX4"
                and safe.get("command") in {"PATROL", "TRACK", "ORBIT", "RTH"}
                and drone.get("battery_valid") is False):
            approved = False
            safe["command"], safe["motion"] = "HOLD", False
            safe["params"] = {"hold_position": True, "alert": "BATTERY_UNVERIFIED"}
            adjustments.append(f"{cmd}->HOLD")
            reason = "BATTERY_UNVERIFIED"

        # A low-battery override must not approve RTL on a stale/disconnected
        # PX4 link. The adapter remains read-only; this also protects decisions.
        if (drone.get("telemetry_source") == "PX4"
                and safe.get("command") in {"PATROL", "TRACK", "ORBIT", "RTH"}
                and (not connected or not (-float("inf") < telemetry_age < float("inf")) or telemetry_age > 5.0)):
            approved = False
            safe["command"], safe["motion"] = "HOLD", False
            safe["params"] = {"hold_position": True, "alert": "PX4_TELEMETRY_LOST"}
            adjustments.append(f"{cmd}->HOLD")
            reason = "PX4_TELEMETRY_LOST"

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

    INPUT  : 외부 텔레메트리 -> TelemetryInputAdapter -> TelemetryState
    OUTPUT : safe_command -> FlightAdapter

    SIM에서는 내부 상태를 사용하고,
    UDP 모드에서는 localhost UDP JSON으로 외부 시뮬레이터/브리지와 통신할 수 있다.
    """
    def __init__(self, telemetry_input, flight_adapter, udp_telemetry_port=14560):
        self.telemetry_input = telemetry_input
        self.flight_adapter = flight_adapter
        self.input_source = getattr(flight_adapter, "name", "SIM")
        self.last_input_at = None
        self.lock = threading.Lock()
        self.udp_telemetry_port = int(udp_telemetry_port)
        self.udp_socket = None

        if self.input_source == "UDP":
            with self.telemetry_input.telemetry.lock:
                self.telemetry_input.telemetry.telemetry_connected = False
                self.telemetry_input.telemetry.telemetry_source = "UDP"
                self.telemetry_input.telemetry.telemetry_last_update = time.monotonic() - 3600
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(("127.0.0.1", self.udp_telemetry_port))
            self.udp_socket.setblocking(False)
            print(
                f"[DRONE IO] UDP telemetry listen -> "
                f"127.0.0.1:{self.udp_telemetry_port}"
            )

    def ingest_telemetry(self, packet, source=None):
        """외부 어댑터가 받은 텔레메트리를 공통 상태로 전달."""
        src = source or self.input_source
        self.telemetry_input.ingest(packet, source=src)
        with self.lock:
            self.last_input_at = time.time()

    def poll_telemetry(self):
        """UDP JSON 텔레메트리를 비차단 방식으로 읽는다."""
        if self.udp_socket is None:
            return None

        latest = None
        # UI 한 프레임에서 오래 막히지 않도록 최대 10개까지만 처리.
        for _ in range(10):
            try:
                data, _addr = self.udp_socket.recvfrom(65535)
            except BlockingIOError:
                break
            except OSError as exc:
                print(f"[DRONE IO] UDP receive error: {exc}")
                break

            try:
                packet = json.loads(data.decode("utf-8"))
                if not isinstance(packet, dict):
                    raise ValueError("telemetry packet must be a JSON object")
                self.ingest_telemetry(packet, source="UDP")
                latest = packet
            except Exception as exc:
                print(f"[DRONE IO] invalid telemetry packet: {exc}")

        if latest is None:
            state = self.telemetry_input.telemetry
            with state.lock:
                if state.telemetry_source == "UDP" and time.monotonic() - state.telemetry_last_update > 5.0:
                    state.telemetry_connected = False
        return latest

    def send_command(self, safe_command, telemetry_state=None):
        """완료된 착륙을 유지하고, RTH/LAND 재전송을 제한한다."""
        mode = str((telemetry_state or {}).get("flight_mode", ""))
        command = str(safe_command.get("command", "HOLD"))
        now = time.monotonic()
        if mode == "LANDED":
            return {
                "adapter": getattr(self.flight_adapter, "name", "UNKNOWN"),
                "connected": bool(getattr(self.flight_adapter, "connected", False)),
                "status": "ACK", "command": "HOLD", "ack": "LANDED_NO_SEND",
                "sent_at": time.time(),
            }
        # 긴급 명령은 주기적으로 재전송하되 UI 프레임마다 반복하지 않는다.
        if (command in {"RTH", "LAND"} and
                getattr(self, "_last_critical_command", None) == command and
                now - getattr(self, "_last_critical_at", -1e9) < 2.0 and
                getattr(self, "_last_critical_result", {}).get("status") in {"ACK", "SENT"}):
            result = dict(self._last_critical_result)
            result["ack"] = "RETRY_THROTTLED"
            return result
        result = self.flight_adapter.send(safe_command)
        if command in {"RTH", "LAND"}:
            self._last_critical_command = command
            self._last_critical_at = now
            self._last_critical_result = dict(result)
        else:
            self._last_critical_command = None
        return result

    def snapshot(self):
        with self.lock:
            return {
                "input_source": self.input_source,
                "last_input_at": self.last_input_at,
                "output_adapter": getattr(self.flight_adapter, "name", "UNKNOWN"),
                "adapter_connected": bool(getattr(self.flight_adapter, "connected", False)),
                "udp_telemetry_port": self.udp_telemetry_port if self.udp_socket else None,
            }

    def close(self):
        if self.udp_socket is not None:
            try:
                self.udp_socket.close()
            except OSError:
                pass
            self.udp_socket = None


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
    """Read-only PX4 SITL observer: NEVER transmits a MAVLink flight command."""
    name = "PX4"

    def send(self, safe_command):
        return self._store_result({
            "adapter": "PX4", "connected": False, "status": "MONITOR",
            "command": safe_command.get("command", "HOLD"),
            "ack": "READ_ONLY_NO_COMMAND_SENT", "sent_at": time.time(),
        })


class Px4TelemetryReceiver(threading.Thread):
    """Read-only MAVLink PX4 observer; no commands or stream requests sent."""
    def __init__(self, telemetry_input, port=14540, system_id=1, component_id=1):
        super().__init__(daemon=True, name="PX4-MAVLink-Receiver")
        self.telemetry_input = telemetry_input
        self.port = int(port)
        from tools.gazebo.mavlink_observer import MavlinkObserver
        self.observer = MavlinkObserver(system_id, component_id)
        self.telemetry_input.telemetry.mavlink_observer = self.observer
        self.stop_event = threading.Event()
        self.last_rx = None
        self.mission_total = None
        self.mission_reached = None
        self.last_mode = None
        self.mission_items_complete = False
        self.mission_seen_progress = False
        # A stored MISSION_CURRENT is not proof of a mission in this app session.
        self.mission_session_started = False
        self.px4_armed = False

    @staticmethod
    def _px4_mode(message, mavutil):
        try:
            mode = mavutil.mode_string_v10(message)
            if mode and mode not in ("UNKNOWN", "Mode(0x00000000)"):
                return str(mode).upper()
        except (ValueError, AttributeError, TypeError):
            pass
        return "UNKNOWN"

    def handle_message(self, msg, mavutil, now=None):
        now = time.monotonic() if now is None else now
        previous_epoch = self.observer.epoch
        mode = self._px4_mode(msg, mavutil) if msg.get_type() == 'HEARTBEAT' else None
        packet = self.observer.ingest(msg, now, mode)
        if previous_epoch != self.observer.epoch:
            self.telemetry_input.disconnect('PX4')
            self.mission_session_started = False
            self.mission_items_complete = False
        if packet is None:
            return False
        self.last_rx = now
        kind = msg.get_type()
        if kind == "HEARTBEAT":
            mode = self._px4_mode(msg, mavutil)
            packet["flight_mode"] = mode
            packet["armed"] = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            # PX4 can repeatedly broadcast the LAST saved mission, even
            # while disarmed in LOITER. Open a progress session only
            # after observing an armed transition into AUTO.MISSION.
            self.px4_armed = packet["armed"]
            new_mission = (self.px4_armed
                           and mode in ("MISSION", "AUTO.MISSION")
                           and (not self.mission_session_started
                                or self.last_mode not in ("MISSION", "AUTO.MISSION")))
            if new_mission:
                self.mission_session_started = True
                self.mission_items_complete = False
                self.mission_seen_progress = False
                self.mission_total = None
                self.mission_reached = None
                self._last_seq = None
                packet["px4_mission_reset"] = True
                print("[PX4 MISSION] new armed mission -> reset stored progress")
            if mode != self.last_mode:
                print(f"[PX4 MODE] {self.last_mode} -> {mode}")
                self.last_mode = mode
            if not self.mission_session_started:
                packet["px4_mission_state"] = "IDLE"
            elif mode in ("MISSION", "AUTO.MISSION") and self.px4_armed:
                packet["px4_mission_state"] = "ACTIVE"
            elif mode in ("RTL", "RETURN", "AUTO.RTL") and self.px4_armed:
                packet["px4_mission_state"] = "RETURNING"
            elif mode in ("LAND", "AUTO.LAND", "LANDING") and self.px4_armed:
                packet["px4_mission_state"] = "LANDING"
            elif not self.px4_armed:
                packet["px4_mission_state"] = (
                    "COMPLETED" if self.mission_items_complete else "STOPPED"
                )
        elif kind in ("GLOBAL_POSITION_INT", "HOME_POSITION", "GPS_RAW_INT", "LOCAL_POSITION_NED"):
            packet.update(position_packet(msg))
        elif kind in ("SYS_STATUS", "BATTERY_STATUS"):
            if 0 <= msg.battery_remaining <= 100:
                packet["battery"] = float(msg.battery_remaining)
        elif kind == "MISSION_CURRENT":
            # Ignore cached mission reports before a mission actually
            # starts, and after disarm. These can stay at seq=2,total=3
            # for the entire next idle period.
            if not (self.mission_session_started and self.px4_armed):
                return False
            seq = int(msg.seq)
            if seq < 0:
                return False
            total = int(getattr(msg, "total", 0) or 0)
            if total > 0:
                self.mission_total = total
                packet["px4_mission_total"] = total
            if self.mission_total and seq >= self.mission_total:
                return False
            packet["px4_mission_seq"] = seq
            mission_state_code = int(getattr(msg, "mission_state", 0) or 0)
            # A saved COMPLETE packet can arrive immediately after
            # switching modes; do not count it as progress in a new run.
            if mission_state_code == 5 and not self.mission_seen_progress:
                print(f"[PX4 MISSION] ignoring cached COMPLETE seq={seq} total={self.mission_total or '?'}")
                return False
            if mission_state_code == 2:
                self.mission_seen_progress = True
            if mission_state_code == 5:
                final_item = (self.mission_total is not None
                              and seq >= self.mission_total - 1)
                if self.mission_seen_progress and final_item:
                    self.mission_items_complete = True
                    packet["px4_mission_items_complete"] = True
                    if self.last_mode not in ("RTL", "RETURN", "AUTO.RTL", "LAND", "AUTO.LAND", "LANDING"):
                        packet["px4_mission_state"] = "COMPLETED"
                else:
                    print(f"[PX4 MISSION] ignoring premature COMPLETE seq={seq} total={self.mission_total or '?'}")
            if getattr(self, '_last_seq', None) != seq:
                print(f"[PX4 MISSION] current seq={seq} total={self.mission_total or '?'}")
            self._last_seq = seq
        elif kind == "MISSION_ITEM_REACHED":
            if not (self.mission_session_started and self.px4_armed):
                return False
            reached_seq = int(msg.seq)
            if reached_seq < 0 or (self.mission_total is not None and reached_seq >= self.mission_total):
                return False
            self.mission_reached = reached_seq
            self.mission_seen_progress = True
            packet["px4_mission_reached"] = reached_seq
            if self.mission_total and reached_seq == self.mission_total - 1:
                self.mission_items_complete = True
                packet["px4_mission_items_complete"] = True
                if self.last_mode not in ("RTL", "RETURN", "AUTO.RTL", "LAND", "AUTO.LAND", "LANDING"):
                    packet["px4_mission_state"] = "COMPLETED"
            print(f"[PX4 MISSION] reached seq={self.mission_reached}")
        if packet:
            self.telemetry_input.ingest(packet, source="PX4")
        return True

    def run(self):
        try:
            from pymavlink import mavutil
        except ImportError:
            print("[PX4] pymavlink missing: python -m pip install pymavlink")
            return
        while not self.stop_event.is_set():
            conn = None
            try:
                conn = mavutil.mavlink_connection(
                    f"udpin:127.0.0.1:{self.port}", source_system=250,
                )
                print(f"[PX4] READ-ONLY MAVLink listening on UDP 127.0.0.1:{self.port}")
                self.last_rx = None
                self.observer.reset()
                self.telemetry_input.disconnect('PX4')
                self.mission_session_started = False
                while not self.stop_event.is_set():
                    msg = conn.recv_match(blocking=True, timeout=0.5)
                    now = time.monotonic()
                    if msg is None:
                        if self.last_rx is None or now - self.last_rx > 5:
                            self.telemetry_input.disconnect("PX4")
                            if self.last_rx is not None:
                                self.observer.reset()
                                self.mission_session_started = False
                                self.last_rx = None
                        continue
                    if msg.get_type() == "BAD_DATA":
                        continue
                    self.handle_message(msg, mavutil, now)
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"[PX4] MAVLink receiver: {exc}; retrying...")
                self.telemetry_input.disconnect("PX4")
                self.stop_event.wait(2.0)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


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


class UdpFlightAdapter(FlightAdapterBase):
    """검증된 safe_command를 localhost UDP JSON으로 외부 프로세스에 전달."""
    name = "UDP"

    def __init__(self, host="127.0.0.1", port=14561):
        super().__init__()
        self.host = str(host)
        self.port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.connect()

    def send(self, safe_command):
        command_name = str(safe_command.get("command", "HOLD"))
        safety = safe_command.get("safety", {})
        approved = bool(safety.get("approved", True))

        if not approved:
            return self._store_result(
                {
                    "adapter": self.name,
                    "connected": True,
                    "status": "BLOCKED",
                    "command": command_name,
                    "ack": safety.get("reason", "SAFETY_BLOCK"),
                    "sent_at": time.time(),
                }
            )

        try:
            outbound = dict(safe_command)
            outbound["command_id"] = f"{time.time_ns()}"
            payload = json.dumps(outbound, ensure_ascii=False).encode("utf-8")
            self.sock.sendto(payload, (self.host, self.port))
            result = {
                "adapter": self.name,
                "connected": True,
                "status": "SENT",
                "command": command_name,
                "command_id": outbound["command_id"],
                "ack": f"UDP_SENT_UNCONFIRMED:{self.host}:{self.port}",
                "params": safe_command.get("params", {}),
                "sent_at": time.time(),
            }
        except OSError as exc:
            result = {
                "adapter": self.name,
                "connected": False,
                "status": "BLOCKED",
                "command": command_name,
                "ack": f"UDP_ERROR:{exc}",
                "sent_at": time.time(),
            }

        return self._store_result(result)


def create_flight_adapter(name, udp_host="127.0.0.1", udp_command_port=14561):
    name = str(name).upper()
    if name == "UDP":
        return UdpFlightAdapter(udp_host, udp_command_port)

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
        flight_mode = str(telemetry_state.get("flight_mode", ""))
        # RTH -> LAND -> LANDED는 각각 새 임무가 아닌 하나의 귀환 임무다.
        rth_sequence = (command in {"RTH", "LAND"} or flight_mode == "LANDED")
        if rth_sequence and (command == "RTH" or flight_mode in {"LAND", "LANDED"}):
            signature = ("RTH_SEQUENCE",)
        else:
            signature = self._signature(safe_command)
        adapter_status = str(adapter_result.get("status", "BLOCKED"))
        read_only_px4 = adapter_result.get("adapter") == "PX4"
        is_udp = adapter_result.get("adapter") == "UDP"
        fresh = bool(telemetry_state.get("telemetry_connected")) and float(telemetry_state.get("telemetry_age_s", 999)) <= 5
        received = str(telemetry_state.get("last_received_command") or "")
        matched = (received == command and
                   str(telemetry_state.get("last_command_id")) == str(adapter_result.get("command_id")))
        # RTH retransmissions may be throttled; mode itself is stronger evidence than send ACK.
        observed = matched or (command == "RTH" and flight_mode in {"RETURNING_HOME", "LAND", "LANDED"}) or (command == "LAND" and flight_mode in {"LAND", "LANDED"}) or (command in {"PATROL", "TRACK", "ORBIT"} and received == command and flight_mode == command and telemetry_state.get("last_command_id") is not None) or (command == "HOLD" and flight_mode in {"HOLD", "LANDED"})

        with self.lock:
            if signature != self.last_signature:
                self.last_signature = signature
                self.started_at = now_mono
                mission_name = "RTH" if signature == ("RTH_SEQUENCE",) else command
                self.mission_id = self._make_mission_id(mission_name)
                self.last_logged_status = None
                self.state = {
                    "mission_id": self.mission_id,
                    "mission": mission_name,
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

            if read_only_px4:
                status = "MONITORING"
                progress = 0
                reason = "PX4_READ_ONLY_NO_COMMAND"
            elif adapter_status not in {"ACK", "SENT"}:
                status = "FAILED"
                progress = 0
                reason = adapter_result.get("ack", "ADAPTER_BLOCKED")

            elif is_udp and not fresh:
                status = "FAILED" if elapsed >= 5.0 else "QUEUED"
                progress = 0
                reason = "TELEMETRY_TIMEOUT" if elapsed >= 5.0 else "WAIT_TELEMETRY"

            elif is_udp and not observed:
                status = "FAILED" if elapsed >= 5.0 else "QUEUED"
                progress = 0
                reason = "COMMAND_NOT_CONFIRMED" if elapsed >= 5.0 else "WAIT_COMMAND_CONFIRMATION"

            elif command == "PATROL":
                status = "RUNNING"
                progress = 0
                reason = "PATROL_ACTIVE"

            elif command in {"ORBIT", "TRACK"}:
                duration = 5.0
                progress = min(100, int((elapsed / duration) * 100))
                status = ("RUNNING" if is_udp and flight_mode == command else
                          ("COMPLETED" if progress >= 100 else "EXECUTING"))
                if is_udp:
                    progress = 0  # 이동 성공을 시간만으로 가정하지 않음
                reason = f"{command}_TELEMETRY" if is_udp else f"{command}_SIM"

            elif command == "RECORD":
                duration = 3.0
                progress = min(100, int((elapsed / duration) * 100))
                status = "COMPLETED" if progress >= 100 else "EXECUTING"
                reason = "RECORD_SIM"

            elif signature == ("RTH_SEQUENCE",):
                if flight_mode == "LANDED":
                    status = "COMPLETED"
                    progress = 100
                    reason = "RTH_LANDED"
                elif flight_mode == "LAND":
                    status = "EXECUTING"
                    progress = 95
                    reason = "RTH_LANDING"
                elif flight_mode == "RETURNING_HOME":
                    status = "EXECUTING"
                    progress = min(90, max(20, int(elapsed * 12)))
                    reason = "RETURNING_HOME"
                else:
                    status = "EXECUTING"
                    progress = min(20, int(elapsed * 10))
                    reason = "RTH_REQUESTED"

            elif command == "HOLD":
                status = "HOLDING"
                progress = 0
                reason = "SAFE_HOLD"

            elif command == "LAND":
                duration = 3.0
                progress = min(100, int((elapsed / duration) * 100))
                status = ("COMPLETED" if flight_mode == "LANDED" else "EXECUTING") if is_udp else ("COMPLETED" if progress >= 100 else "EXECUTING")
                if is_udp:
                    progress = 100 if flight_mode == "LANDED" else 50
                reason = "LAND_TELEMETRY" if is_udp else "LAND_SIM"

            else:
                status = "EXECUTING"
                progress = 0
                reason = "COMMAND_ACTIVE"

            previous_status = self.state.get("status")
            self.state.update(
                {
                    "mission_id": self.mission_id,
                    "mission": self.state.get("mission", command),
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
        # 외부 UDP 상태를 내부 시간 기반 SIM 상태 머신이 덮어쓰지 않도록 한다.
        if self.telemetry.snapshot().get("telemetry_source") == "SIM":
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

        if (level == "RTH" and state["flight_mode"] not in {"RETURNING_HOME", "LAND", "LANDED"}
                and state["telemetry_source"] == "SIM" and self.telemetry.request_rth()):
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


def likely_sky_cloud_smoke(frame, rect, fire_present=False):
    """Conservative heuristic for cloud/blue-sky false positives (not a smoke classifier).

    Only veto an upper-frame candidate if its rectangle includes substantial clear
    blue sky. Fire evidence always bypasses this filter. Genuine outdoor smoke
    can overlap sky, so field validation is essential.
    """
    if fire_present or frame is None:
        return False
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in rect)
    x1, x2 = max(0, x1), min(width, x2)
    y1, y2 = max(0, y1), min(height, y2)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return False
    # Require the candidate to be predominantly in the upper sky area.
    if (y1 + y2) * 0.5 > height * 0.40 or y2 > height * 0.58:
        return False
    roi = frame[y1:y2:4, x1:x2:4]
    if roi.size == 0:
        return False
    b = roi[:, :, 0].astype(np.int16)
    g = roi[:, :, 1].astype(np.int16)
    r = roi[:, :, 2].astype(np.int16)
    # Blue-dominant, sufficiently bright pixels indicate clear sky background.
    blue_sky = (b > 115) & (b > r + 23) & (b > g + 8) & (g > 65)
    return float(np.mean(blue_sky)) >= 0.28


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
    """검증되지 않은 지리 방향 대신 화면 방향 이름을 기록한다."""
    return direction if direction in ("UP", "DOWN", "LEFT", "RIGHT", "LEFT_UP", "RIGHT_UP",
        "LEFT_DOWN", "RIGHT_DOWN", "SPREAD", "SPREAD_UP", "SPREAD_DOWN") else "UNKNOWN"

# Screen motion is not a geographic wind bearing unless camera orientation is calibrated.
_SMOKE_SCREEN_BEARINGS = {
    "UP": 0.0, "RIGHT_UP": 45.0, "RIGHT": 90.0,
    "RIGHT_DOWN": 135.0, "DOWN": 180.0, "LEFT_DOWN": 225.0,
    "LEFT": 270.0, "LEFT_UP": 315.0,
}
_SMOKE_COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def estimated_smoke_bearing(screen_direction, heading_deg, camera_yaw_offset_deg=None):
    """Return approximate FROM-image plume travel bearing, clockwise from true north.

    Only available when the operator explicitly provides a camera calibration offset.
    Assumes fixed forward-oriented, non-mirrored image and negligible camera roll;
    perspective, gimbal tilt, changing camera yaw, and wind drift are NOT corrected.
    This is NOT a measured wind direction.
    """
    if camera_yaw_offset_deg is None or heading_deg is None:
        return None
    screen_bearing = _SMOKE_SCREEN_BEARINGS.get(screen_direction)
    if screen_bearing is None:
        return None
    try:
        bearing = (float(heading_deg) + float(camera_yaw_offset_deg) + screen_bearing) % 360.0
        if not np.isfinite(bearing):
            return None
        return bearing
    except (TypeError, ValueError):
        return None


def smoke_bearing_label(bearing):
    if bearing is None:
        return None
    return _SMOKE_COMPASS_8[int((bearing + 22.5) // 45) % 8]

def fit_image(image, width, height, fill=False):
    """Return displayed tile, display rectangle, and visible source crop."""
    h, w = image.shape[:2]
    if fill:
        # Center crop in source pixels, then resize uniformly to the tile.
        target = width / height
        if w / h > target:
            ch = h;cw = max(1, round(h * target));cx = (w - cw) // 2;cy = 0
        else:
            cw = w;ch = max(1, round(w / target));cx = 0;cy = (h - ch) // 2
        resized = cv2.resize(image[cy:cy + ch, cx:cx + cw], (width, height),
                             interpolation=cv2.INTER_AREA if width < cw else cv2.INTER_CUBIC)
        return resized, (0, 0, width, height), (cx, cy, cw, ch)
    scale = min(width / w, height / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    nw, nh = min(width, nw), min(height, nh)
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(image, (nw, nh), interpolation=interpolation)
    canvas = np.full((height, width, 3), 22, np.uint8)
    ox, oy = (width - nw) // 2, (height - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas, (ox, oy, nw, nh), (0, 0, w, h)

def tile_to_frame(x, y, display_rect, source_crop, frame_size):
    mx, my, mw, mh = display_rect
    cx, cy, cw, ch = source_crop
    fw, fh = frame_size
    return (int(np.clip(cx + (x - mx) * cw / mw, 0, fw - 1)),
            int(np.clip(cy + (y - my) * ch / mh, 0, fh - 1)))

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
        self.analysis_lock = threading.RLock()
        self.source_generation, self.analysis_generation = 0, None
        self.pending, self.sequence, self.done = None, 0, -1
        self.overlay_mask, self.overlay_time, self.received_at = None, 0.0, 0.0
        self.analysis_sequence, self.analysis_received_at, self.analysis_done_at = -1, 0.0, 0.0
        self.presentation_pair = None
        self.analysis_ms, self.analysis_fps, self.display_fps = 0.0, 0.0, 0.0
        self.analysis_rate, self.display_rate = RateMeter(), RateMeter()
        self.display_sequence = -1
        self.auto_lock = threading.Lock()
        self.auto_control = AutoSafetyController()
        self.auto_result = self.auto_control.latest
        self.auto_low_score_rematch = False
        self.auto_roi_panel = AutoRoiPreview()
        self.label_anchors = {}
        self.recent_fire_candidates, self.recent_fire_candidates_at = [], 0.0
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
        self.hazard_fusion = HazardFusion()
        self.fire_persistence = FirePersistence()
        self.fire_scene_guard = SceneMotionGuard()
        self.fire_temporal_confirmed = []
        self.fire_last_check = -float("inf")
        self.fire_check_sequence = 0
        self.show_fire_smoke = True
        self.show_direction_overlay = True  # L: compact, image-space estimate only
        self.fire_hits = self.smoke_hits = self.crowd_hits = 0

        # SMOKE 이동 방향은 먼저 "화면 기준"으로 계산한다.
        # 실제 동/서/남/북은 추후 드론 heading(yaw)과 결합한다.
        self.smoke_centers = deque(maxlen=8)
        self.smoke_direction = "UNKNOWN"
        self.smoke_motion_estimator = SmokeMotion()
        self.smoke_motion_source = (source, None)
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
    def analysis_is_current(self):
        return (not self.stop_event.is_set()
                and getattr(self, 'processing_generation', self.source_generation) == self.source_generation)

    def invalidate_connection(self, status, error=None):
        with self.lock:
            self.source_generation += 1
            self.status, self.connection_error = status, error
            self.pending = self.raw = self.image = self.presentation_pair = None
            self.objects, self.selected_id, self.roi, self.danger = {}, None, None, None
            self.overlay_mask = None
            for key in ('automation_capture','camera_bridge_metadata','presentation_age_ms','presentation_sequence','presentation_objects'):
                self.__dict__.pop(key, None)
            self.fire_until = self.smoke_until = self.crowd_until = 0.0
            self.previous_danger, self.fall_until = set(), {}

    def begin_connection(self, status='Reconnecting'):
        # Invalidate before waiting: the UI never waits for an in-flight inference.
        self.invalidate_connection(status)
        with self.analysis_lock:
            fresh = Channel(self.number, '', self.show_ppe, self.event_manager)
            with self.lock:
                keep = {k: self.__dict__[k] for k in (
                    'number', 'source', 'event_manager', 'lock', 'analysis_lock',
                    'stop_event', 'process_lock', 'process', 'reader', 'source_generation',
                    'show_ppe', 'show_fire_smoke', 'show_direction_overlay',
                    'site_mode', 'auto_low_score_rematch')}
                if hasattr(self, 'camera_bridge_metrics'):keep['camera_bridge_metrics'] = self.camera_bridge_metrics
                self.__dict__.clear()
                self.__dict__.update(fresh.__dict__)
                self.__dict__.update(keep)
                self.status = 'Stopped' if self.stop_event.is_set() else status
                self.smoke_motion_source = (self.source, None)

    def log(self, kind, track_id, item=None):
        if not self.analysis_is_current():return
        folder = ROOT / "data" / "multi_tracker" / f"camera_{self.number}"
        folder.mkdir(parents=True, exist_ok=True)  # 영상별 CSV: 동일 ID가 섞이지 않음
        path = folder / f"{kind}_events.csv"
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(["time", "track_id"] + (["missing_item"] if item else []))
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), track_id] + ([item] if item else []))
    def publish(self, frame, generation=None):
        now = time.monotonic()
        with self.lock:
            if self.stop_event.is_set() or (generation is not None and generation != self.source_generation):return False
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
        self.invalidate_connection("Stopped")
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()  # FFmpeg 읽기 대기도 즉시 해제
                except OSError:
                    pass
    def read_ffmpeg(self, url, executable):
        generation = self.source_generation
        command = [executable, "-hide_banner", "-loglevel", "warning", "-nostdin", "-rw_timeout", "15000000"]
        if url.startswith(("https://", "http://")):
            command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
        if ".m3u8" in url:
            command += ["-live_start_index", "-2"]  # 라이브 목록의 끝부분에서 시작
        if url.startswith("rtsp://"):
            command += ["-rtsp_transport", "tcp"]
        command += ["-re", "-i", url, "-an", "-sn", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        return self.read_rawvideo(command, generation)

    def read_camera(self, index):
        from tools.control_ui.camera_input import capture_command
        if self.stop_event.is_set():return
        return self.read_rawvideo(capture_command(index), self.source_generation, camera=True)

    def read_rawvideo(self, command, generation, camera=False):
        # Shared bounded decoder lifecycle for network streams and video-only webcams.
        if self.stop_event.is_set() or generation != self.source_generation:return
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        errors = deque(maxlen=32)
        def read_errors():
            while True:
                chunk = process.stderr.read(2048)
                if not chunk:break
                errors.append(chunk.decode(errors='replace'))
        error_reader = threading.Thread(target=read_errors, daemon=True)
        error_reader.start()
        failure = None
        received_frames = 0
        forced = [False]
        with self.process_lock:
            self.process = process
        finished = threading.Event()
        last_data = [time.monotonic()]
        def watchdog():
            while not finished.wait(0.5):
                if self.stop_event.is_set() or time.monotonic() - last_data[0] > 25:
                    if process.poll() is None:
                        try:
                            process.terminate()
                            try:process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                forced[0] = True
                                process.kill()
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
                self.publish(np.frombuffer(data, dtype=np.uint8).reshape(720, 1280, 3).copy(), generation)
                received_frames += 1
        except Exception as exc:
            failure = exc
        finally:
            finished.set()
            if process.poll() is None:
                try:process.terminate()
                except OSError:pass
            # Release the producer's pipe before waiting, so a full stdout cannot
            # keep an otherwise cooperative decoder alive during shutdown.
            process.stdout.close()
            try:exit_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                forced[0] = True
                process.kill();exit_code = process.wait()
            guard.join(timeout=1)
            error_reader.join(timeout=1)
            process.stderr.close()
            self.decoder_cleanup = dict(pid=process.pid,exit_code=exit_code,forced_termination=forced[0],process_exited=process.poll() is not None,
                                        stop_requested=self.stop_event.is_set(),frames=received_frames,stdout_closed=process.stdout.closed,
                                        stderr_closed=process.stderr.closed,threads_exited=not guard.is_alive() and not error_reader.is_alive(),
                                        stderr=''.join(errors)[-2000:])
            with self.process_lock:
                if self.process is process:
                    self.process = None
        if failure is not None and not self.stop_event.is_set():
            detail = ''.join(errors)[-2000:]
            if any(word in detail.lower() for word in ('permission','not authorized','denied','not permitted')):
                raise PermissionError('CAMERA/SOURCE_PERMISSION_DENIED: '+detail)
            if camera and received_frames==0:raise ValueError('CAMERA_OPEN_FAILED / reconnect manually: '+detail)
            raise RuntimeError(str(failure)+': '+detail)
    def read_gazebo(self):
        generation = self.source_generation
        from tools.gazebo.camera_source import frames
        topic = self.source[len("gazebo://"):]
        if not topic.startswith("/"):
            raise ValueError("Gazebo source must be gazebo:///safety/probe/image")
        def process_changed(process):
            with self.process_lock:
                if self.stop_event.is_set() or generation != self.source_generation:return
                self.process = process
        stream = frames(topic, self.stop_event, process_changed)
        try:
            for frame in stream:
                if self.stop_event.is_set():
                    break
                self.publish(frame, generation)
        finally:
            stream.close()

    def read_gazebo_ssh(self):
        generation = self.source_generation
        # Image-only SSH; no MAVLink, GCS or flight adapter is created here.
        from tools.gazebo.camera_bridge import frames, Metrics
        metrics = Metrics()
        self.camera_bridge_metrics = metrics
        def process_changed(process):
            with self.process_lock:
                if self.stop_event.is_set() or generation != self.source_generation:return
                self.process = process
        def state_changed(state):
            nonlocal generation
            if state != 'PLAYING':
                with self.lock:
                    if self.stop_event.is_set() or generation != self.source_generation:return
                self.begin_connection('Camera ' + state)
                generation = self.source_generation
        stream = frames(self.source, self.stop_event, process_changed, state_changed, metrics)
        try:
            for frame, metadata in stream:
                if self.stop_event.is_set():
                    break
                self.camera_bridge_metadata = metadata
                self.publish(frame, generation)
        finally:
            stream.close()

    def read_loop(self):
        while not self.stop_event.is_set():
            cap = None
            try:
                if self.source.startswith("gazebo-ssh://"):
                    self.read_gazebo_ssh()
                    continue
                if self.source.startswith("gazebo://"):
                    self.read_gazebo()
                    continue
                generation = self.source_generation
                resolved = resolve_source(self.source)
                if self.stop_event.is_set() or generation != self.source_generation:return
                if type(resolved) is int and sys.platform == "darwin":
                    self.read_camera(resolved)
                    continue
                local = isinstance(resolved, str) and "://" not in resolved
                if local and not Path(resolved).is_file():
                    raise FileNotFoundError(resolved)
                if isinstance(resolved, str) and "://" in resolved:
                    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
                    ffmpeg = shutil.which(ffmpeg_name, path=str(Path(sys.executable).parent))
                    ffmpeg = ffmpeg or shutil.which(ffmpeg_name)
                    if ffmpeg:
                        self.read_ffmpeg(resolved, ffmpeg)
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
                                if not self.stop_event.is_set() and generation == self.source_generation:self.status = "Ended"
                            return  # 파일 끝에서는 마지막 화면 유지
                        raise RuntimeError("Stream disconnected")
                    self.publish(frame, generation)
                    if interval:
                        deadline += interval
                        self.stop_event.wait(max(0, deadline - time.monotonic()))
            except (PermissionError, ValueError) as error:
                if self.stop_event.is_set():return
                self.invalidate_connection('Permission denied / reconnect manually' if isinstance(error, PermissionError) else 'Camera/source rejected', str(error))
                return
            except Exception as error:
                if self.stop_event.is_set():return
                self.invalidate_connection('Connection failed / retrying', str(error))
                print(f"[Camera {self.number}] {error}")
                if not self.stop_event.wait(3):self.begin_connection()
            finally:
                if cap is not None:
                    cap.release()  # 읽기 스레드에서만 해제: 다른 스레드와 충돌 방지

class Analyzer(threading.Thread):
    def __init__(self, channels, device):
        super().__init__(daemon=True)
        self.channels, self.device, self.stop_event = channels, device, threading.Event()
        self.error = None
        self.smoke_camera_yaw_offset = None  # Explicit opt-in: no uncalibrated north claims.
    def run(self):
        try:
            models = {}  # 새 영상으로 바꾸면 추적기도 새로 생성
            pose = YOLO(str(MODEL_ROOT / "yolo11n-pose.pt"))
            ppe = YOLO(str(MODEL_ROOT / "ppe_best.pt"))  # P키로 나중에 켜도 모델을 사용할 수 있음
            fire_path = MODEL_ROOT / "fire_smoke_best.pt"
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
                    with channel.analysis_lock:
                        with channel.lock:
                            frame, sequence = channel.pending, channel.sequence
                            channel.processing_received_at = channel.received_at
                            generation = channel.source_generation
                            channel.processing_generation = generation
                        if channel.stop_event.is_set() or frame is None or sequence == channel.done:
                            continue
                        try:
                            if channel not in models or models[channel][0] != generation:
                                models[channel] = (generation, YOLO(str(MODEL_ROOT / "yolo11s.pt")))
                            started = time.perf_counter()
                            channel.processing_sequence = sequence
                            self.process(channel, models[channel][1], pose, ppe, fire_smoke, frame)
                        except Exception:
                            if not channel.analysis_is_current():continue
                            raise
                        if not channel.analysis_is_current():continue
                        channel.selected_sequence_skips = getattr(channel, "selected_sequence_skips", 0) + max(0, sequence-channel.done-1)
                        channel.done = sequence
                        channel.analysis_completed_count = getattr(channel, "analysis_completed_count", 0) + 1
                        channel.fps = 1 / max(time.perf_counter() - started, 1e-6)
                        finished = time.monotonic()
                        with channel.lock:
                            if not channel.analysis_is_current():continue
                            channel.analysis_generation = generation
                            channel.analysis_sequence = sequence
                            channel.analysis_received_at = channel.processing_received_at
                            channel.analysis_done_at = finished
                            channel.analysis_ms = (time.perf_counter() - started) * 1000
                            channel.analysis_rate.mark(finished)
                            channel.analysis_fps = channel.analysis_rate.fps(finished)
                        worked = True
                if not worked:
                    self.stop_event.wait(0.01)
        except Exception as error:
            self.error = str(error)
            print("AI 오류:", error)
    def process(self, c, model, pose, ppe, fire_smoke, frame):
        process_started = time.monotonic()
        tracker_file = str(LOW_SCORE_TRACKER_CONFIG) if c.auto_low_score_rematch else "bytetrack.yaml"
        person_conf = 0.10 if c.auto_low_score_rematch else 0.35
        result = model.track(frame, device=self.device, persist=True, tracker=tracker_file, conf=person_conf, iou=0.5, classes=[0, 2, 3, 5, 7], imgsz=832, verbose=False)[0]
        if not c.analysis_is_current():return
        objects = {}
        for box in result.boxes:
            if c.auto_low_score_rematch and int(box.cls[0]) != 0 and float(box.conf[0]) < 0.35:
                continue  # Vehicle visibility keeps the existing threshold in the opt-in experiment.
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
        # Preserve the ten-frame cadence for fast sources, cap slow-stream gaps at 0.5s.
        fire_inspect = inspect or time.monotonic() - c.fire_last_check >= .5
        if not c.show_fire_smoke or fire_smoke is None:
            c.fire_result = None
            c.hazard_fusion = HazardFusion()
            c.fire_persistence = FirePersistence()
            c.fire_scene_guard = SceneMotionGuard()
        if fire_inspect:
            c.fire_result = (
                fire_smoke.predict(frame, conf=0.30, imgsz=640, device=self.device, verbose=False)[0]
                if fire_smoke is not None and c.show_fire_smoke else None
            )
            c.fire_last_check = time.monotonic()
            c.fire_check_sequence += 1
        fire_boxes = []
        if c.fire_result is not None:
            for box in c.fire_result.boxes:
                name = str(c.fire_result.names[int(box.cls[0])]).lower()
                if ("fire" in name or "flame" in name) and float(box.conf[0]) >= .30:
                    fire_boxes.append(tuple(map(float, box.xyxy[0].tolist())))
        if fire_inspect:
            c.hazard_fusion.update(c.fire_check_sequence, time.monotonic(), fire_boxes)
            excluded = [tuple(map(float,b.xyxy[0].tolist())) for b in c.fire_result.boxes] if c.fire_result is not None else []
            stable = c.fire_scene_guard.update(frame, excluded)
            key = hashlib.blake2b(frame.tobytes(),digest_size=16).hexdigest()
            c.fire_temporal_confirmed = c.fire_persistence.update(
                getattr(c,"processing_sequence",c.sequence), time.monotonic(), fire_boxes, key, stable)
        scores = {int(box.id[0]): float(box.conf[0]) for box in result.boxes if box.id is not None}
        objects = c.hazard_fusion.filter_people(objects, scores)
        if self.device == "mps":
            torch.mps.synchronize()  # 모든 모델 추론은 한 스레드에서 순서대로 실행
        image = frame.copy()
        layout = DetectionLabelLayout((image.shape[1], image.shape[0]), c.label_anchors)

        safety_candidates = []
        # FIRE / SMOKE 시각화
        # 후보는 얇은 박스, 확정 이벤트 상태에서는 굵은 박스로 표시한다.
        if c.fire_result is not None:
            for box in c.fire_result.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])

                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                raw_name = str(c.fire_result.names.get(cls_id, cls_id))
                name = raw_name.lower().replace(" ", "_")

                if "smoke" in name:
                    # 알림/AI 증거 자격은 그대로 유지한다. 영상 주석만 그리지 않는다.
                    if (conf >= c.smoke_conf_threshold and not likely_sky_cloud_smoke(
                            frame, (x1,y1,x2,y2), fire_present=bool(fire_boxes) or time.time()<c.fire_until)):
                        safety_candidates.append(dict(kind="SMOKE", confidence=conf,
                            xyxy=[x1,y1,x2,y2], display_eligible=True, video_annotation_visible=False))
                    continue
                if not ("fire" in name or "flame" in name) or conf < .30:
                    continue
                color = (0,0,255)
                safety_candidates.append(dict(kind="FIRE",confidence=conf,
                    xyxy=[x1,y1,x2,y2],display_eligible=True))
                cv2.rectangle(image,(x1,y1),(x2,y2),color,2 if time.time()<c.fire_until else 1)
                layout.add(f"FIRE:{round((x1+x2)/128)}:{round((y1+y2)/128)}",
                           ("FIRE","F"),(x1,y1,x2,y2),color,100)

        direction_badge = None
        # 매 분석 프레임의 실제 움직임만 사용한다. 박스 중심/화재 위치 벡터는 이동이 아니다.
        smoke_regions = []
        exclusions = list(fire_boxes) + [value[1] for value in objects.values()]
        if c.fire_result is not None and time.monotonic()-c.fire_last_check <= 1.0:
            for box in c.fire_result.boxes:
                label = str(c.fire_result.names[int(box.cls[0])]).lower()
                rect = box.xyxy[0].tolist()
                if ("smoke" in label and float(box.conf[0]) >= c.smoke_conf_threshold
                        and not likely_sky_cloud_smoke(frame, rect, fire_present=bool(fire_boxes) or time.time()<c.fire_until)):
                    smoke_regions.append(rect)
        motion_source = (c.source, (getattr(c,'camera_bridge_metadata',None) or {}).get('stream_id'))
        if motion_source != c.smoke_motion_source:
            c.smoke_motion_estimator = SmokeMotion()
            c.smoke_motion_source = motion_source
        motion_time = getattr(c,'processing_received_at',0.0) or time.monotonic()
        c.smoke_motion_estimator.update(frame, smoke_regions, motion_time, exclusions)
        motion = c.smoke_motion_estimator.snapshot(time.monotonic())
        c.smoke_direction = motion['direction']
        c.smoke_motion = ((motion.get('dx_px_s') or 0)**2+(motion.get('dy_px_s') or 0)**2)**.5 / motion.get('fps',1)
        if c.show_direction_overlay:
            direction_badge = (max(0,image.shape[1]-190),max(0,image.shape[0]-140),image.shape[1],image.shape[0]-40)


        now = time.time()

        # FIRE / SMOKE: cached results never count as new temporal evidence.
        if fire_inspect and c.fire_result is not None:
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
                    sx1, sy1, sx2, sy2 = map(int, box.xyxy[0].tolist())
                    if likely_sky_cloud_smoke(
                        frame, (sx1, sy1, sx2, sy2),
                        fire_present=(bool(fire_boxes) or time.time() < c.fire_until),
                    ):
                        continue  # Do not advance the confirmed smoke alarm counter.
                    smoke_conf = max(smoke_conf, conf)
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

            c.fire_hits = FIRE_CONFIRM_HITS if (c.hazard_fusion.confirmed_fire or c.fire_temporal_confirmed) else 0

            smoke_required_hits = 6 if c.night_mode else SMOKE_CONFIRM_HITS
            smoke_ok = smoke_conf >= c.smoke_conf_threshold
            c.smoke_hits = c.smoke_hits + 1 if smoke_ok else max(0, c.smoke_hits - 1)

            if c.fire_hits >= FIRE_CONFIRM_HITS:
                c.fire_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(
                        c, "FIRE", fire_conf,
                        details={"confirmed_hits": c.fire_hits}, frame=frame,
                    )
                c.fire_hits = 0

            if c.smoke_hits >= smoke_required_hits:
                c.smoke_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(
                        c,
                        "SMOKE",
                        smoke_conf,
                        frame=frame,
                        details={
                            "confirmed_hits": c.smoke_hits,
                            "screen_direction": c.smoke_direction,
                            "screen_compass": smoke_screen_compass(c.smoke_direction),
                            "screen_motion_px_per_check": round(c.smoke_motion, 2),
                            "scene_brightness": round(c.scene_brightness, 1),
                            "night_mode": c.night_mode,
                            "smoke_conf_threshold": round(c.smoke_conf_threshold, 2),
                            "compass_direction": "UNKNOWN",
                            "heading_required": True,
                            "bearing_method": "SCREEN_BACKGROUND_FLOW",
                            "screen_motion_evidence": c.smoke_motion_estimator.snapshot(time.monotonic()),
                            "camera_yaw_offset_deg": self.smoke_camera_yaw_offset,
                            "bearing_note": "화면 이동만 기록. 지리 방향·바람 방향은 미검증",
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
            image = c.ppe_result.plot(img=image, labels=False, conf=False, line_width=1)
            from ultralytics.utils.plotting import colors as yolo_colors
            for index, box in enumerate(c.ppe_result.boxes):
                item = int(box.cls[0])
                rect = tuple(map(int, box.xyxy[0].tolist()))
                label = str(c.ppe_result.names.get(item, item)).upper().replace(" ", "_")
                layout.add(f"PPE:{item}:{index}", (label, label[:3]), rect,
                           yolo_colors(item, True), 50)
        with c.lock:
            danger = c.danger
        inside = set()
        for tid, (kind, rect) in sorted(objects.items()):
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
            if kind == 0:
                layout.add(f"HUMAN:{tid}", (f"ID:{tid}", str(tid)), rect, color, 10)
            else:
                layout.add(f"{name}:{tid}", (f"{name} ID:{tid}", f"ID:{tid}"), rect,
                           color, 20)
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
                        details={"people": person_count, "danger_people": danger_count, "risk": risk}, frame=frame,
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
        if c.night_mode and c.show_fire_smoke and not globals().get('SUPPRESS_INLINE_LOW_LIGHT', False):
            warnings += [f"LOW LIGHT MODE | SMOKE THR:{c.smoke_conf_threshold:.2f}"]
        warnings += [f"POSSIBLE FALL ID:{tid}" for tid in list(c.fall_until)[:3]]
        reserved = [(0, 0, min(360, image.shape[1]), min(image.shape[0], 12 + len(warnings) * 20))]
        if direction_badge is not None:
            reserved.append(direction_badge)
        layout.draw(image, reserved=reserved)
        c.label_anchors = layout.anchors
        for row, text in enumerate(warnings):
            cv2.putText(image, text, (15, 22 + row * 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.42, (0, 0, 255), 1, cv2.LINE_AA)
        auto_result = None
        if c.auto_control.enabled:
            auto_now = time.monotonic()
            tracked_scores = {int(box.id[0]): float(box.conf[0]) for box in result.boxes
                              if box.id is not None}
            people_tracks = [dict(id=tid, class_id=0, xyxy=rect,
                                  confidence=tracked_scores.get(tid, 0.35))
                             for tid, (kind, rect) in objects.items() if kind == 0]
            candidates = [Candidate("HUMAN", rect, tracked_scores.get(tid, 0.35), track_id=tid)
                          for tid, (kind, rect) in objects.items() if kind == 0]
            if inspect:
                c.recent_fire_candidates = []
                for box in c.fire_result.boxes if c.fire_result is not None else ():
                    name = str(c.fire_result.names[int(box.cls[0])]).lower()
                    conf = float(box.conf[0])
                    rect = tuple(map(float, box.xyxy[0].tolist()))
                    if ("fire" in name or "flame" in name) and conf >= 0.35:
                        c.recent_fire_candidates.append(Candidate("FIRE", rect, conf, "YOLO_FULL"))
                    elif ("smoke" in name and conf >= c.smoke_conf_threshold
                          and not likely_sky_cloud_smoke(frame, rect, fire_present=now < c.fire_until)):
                        c.recent_fire_candidates.append(Candidate("SMOKE", rect, conf, "YOLO_FULL"))
                c.recent_fire_candidates_at = auto_now
            if auto_now - c.recent_fire_candidates_at <= 0.6:
                candidates.extend(Candidate(item.kind, item.xyxy, item.confidence,
                                            item.source if inspect else "YOLO_RECENT")
                                  for item in c.recent_fire_candidates)
            if now < c.crowd_until and people_tracks:
                boxes = [item["xyxy"] for item in people_tracks]
                bounds = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                          max(box[2] for box in boxes), max(box[3] for box in boxes))
                candidates.append(Candidate("CROWD", bounds, 1.0, "LEGACY_CONFIRMED"))
            confirmed_kinds = {kind for kind, until in (("FIRE", c.fire_until),
                               ("SMOKE", c.smoke_until), ("CROWD", c.crowd_until)) if now < until}
            telemetry = c.event_manager.telemetry.snapshot() if c.event_manager else {}
            px4_connected = (telemetry.get("telemetry_source") == "PX4"
                             and telemetry.get("telemetry_connected")
                             and float(telemetry.get("telemetry_age_s", 999)) <= 5)
            with c.auto_lock:
                auto_result = c.auto_control.update(
                    now=auto_now, width=frame.shape[1], height=frame.shape[0],
                    candidates=candidates, tracks=people_tracks,
                    source_age_ms=max(0, (auto_now - getattr(c, "processing_received_at", auto_now)) * 1000),
                    analysis_ms=(time.monotonic() - process_started) * 1000,
                    confirmed_kinds=confirmed_kinds, px4_connected=bool(px4_connected))
                if auto_result["roi_retry_allowed"]:
                    c.auto_control.note_retry(auto_now)
            if auto_result["roi_retry_allowed"] and fire_smoke is not None:
                retry_started = time.monotonic()
                try:
                    retry_boxes = fire_roi_retry(frame, auto_result["digital_roi"], fire_smoke, self.device)
                    if self.device == "mps":
                        torch.mps.synchronize()
                    auto_result["roi_candidates"] = [dict(kind=b.kind, xyxy=b.xyxy,
                                                          confidence=round(b.confidence, 3),
                                                          source=b.source) for b in retry_boxes]
                    tx, ty = auto_result["target"]
                    auto_result["roi_same_kind_at_target"] = any(
                        b.kind == auto_result["target_kind"]
                        and b.confidence >= (0.35 if b.kind == "FIRE" else c.smoke_conf_threshold)
                        and b.xyxy[0] <= tx <= b.xyxy[2] and b.xyxy[1] <= ty <= b.xyxy[3]
                        for b in retry_boxes)
                except Exception as exc:
                    auto_result["roi_error"] = str(exc)
                auto_result["roi_retry_ms"] = (time.monotonic() - retry_started) * 1000
        evidence = getattr(c.event_manager, "safety_evidence", None)
        if inspect and evidence is not None and c.analysis_is_current():
            evidence.observe(channel=c.number, sequence=c.frame_count,
                             captured_at=getattr(c, "processing_received_at", 0.0),
                             now=time.monotonic(), candidates=safety_candidates)
        mask = np.any(image != frame, axis=2)  # 원본 배경을 제외하고 감지 선·글자만 분리
        if auto_result is not None:
            # The crop and ROI coordinates must come from this same analysis frame.
            with c.auto_lock:
                if c.auto_control.enabled:
                    c.auto_roi_panel.capture(frame, auto_result, c.processing_received_at,
                                             c.processing_sequence)
        with c.lock:
            if not c.analysis_is_current():return
            c.overlay_mask, c.overlay_time = mask, time.monotonic()
            c.raw, c.image, c.objects = frame, image, objects
            # Immutable presentation references: never composite these boxes onto another frame.
            c.presentation_pair = (c.processing_sequence, c.processing_received_at,
                                   frame, image, objects, c.overlay_time)
            if auto_result is not None:
                c.auto_result = auto_result
            c.people = sum(kind == 0 for kind, _ in objects.values())
            c.vehicles = sum(int(box.cls[0]) in VEHICLES and float(box.conf[0]) >= 0.35
                             for box in result.boxes)

def live_snapshot(channel):
    with channel.lock:
        stop = getattr(channel, 'stop_event', None)
        if stop is not None and (stop.is_set() or channel.status not in ('Playing', 'Ended')):
            return None, None, {}
        if hasattr(channel, 'source_generation') and channel.analysis_generation != channel.source_generation:
            raw = channel.pending
            return raw, raw.copy() if raw is not None else None, {}
        pair = getattr(channel, 'presentation_pair', None)
        if hasattr(channel, 'camera_bridge_metrics') and pair is not None:
            sequence, captured, raw, annotated, paired_objects, completed = pair
            now = time.monotonic()
            channel.presentation_sequence = sequence
            channel.presentation_objects = paired_objects
            channel.presentation_age_ms = max(0, (now-captured)*1000)
            channel.presentation_stale = (now-channel.received_at >= 2 or now-captured >= 2)
            # Keep the exact analyzed frame+boxes together until the next completed pair.
            # Interaction freshness remains the existing 0.7s rule; no safety/event updates here.
            interactive = paired_objects if now-completed <= 0.7 else {}
            return raw, annotated.copy(), interactive
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
    if code in [ord(c) for c in "1234VSZMDRPOUQFKHGJBNAYTILC"]:
        return code + 32 if 65 <= code <= 90 else code
    text = event.text().lower()
    if len(text) == 1 and text in "1234vszmdrpouqfkhgjbnaytilc":
        return ord(text)
    if sys.platform == "darwin":
        physical = {18: "1", 19: "2", 20: "3", 21: "4", 9: "v", 1: "s", 6: "z", 46: "m", 2: "d", 15: "r", 35: "p", 31: "o", 32: "u", 12: "q", 3: "f", 40: "k", 4: "h", 5: "g", 38: "j", 11: "b", 45: "n", 0: "a", 16: "y", 17: "t", 34: "i", 37: "l", 8: "c", 53: "\x1b"}
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
            def paintEvent(self, event):
                owner.paint_count += 1
                super().paintEvent(event)
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
        self.paint_count = 0
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
        self.window.showMaximized()  # 시작 시 사용 가능한 화면을 최대한 활용
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
        h, w = image.shape[:2]
        # QPixmap.fromImage copies the pixels before the temporary NumPy canvas
        # is released; avoid a separate BGR->RGB array and QImage copy.
        qimage = self.QImage(image.data, w, h, image.strides[0], self.QImage.Format_BGR888)
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
                elif code in [ord(c) for c in "1234VSZMDRPOUQFKHTILC"]:
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
        self.risk_engine = RiskAlertEngine()
        self.current_risk = self.risk_engine.current
        self.selected, self.layout = 0, 1
        self.last_multi_layout = 2
        self.maps, self.tile_maps, self.drag_channel = {}, {}, None
        self.dialog_app, self.retired = None, []
        self.floating_zoom = None
        self.main_view = None
        self.show_map = False
        self.show_auto_debug = False
        self.key_queue = deque()
    def ensure_qt(self):
        from PySide6.QtWidgets import QApplication
        self.dialog_app = QApplication.instance() or QApplication([])
        self.dialog_app.setQuitOnLastWindowClosed(False)
        return self.dialog_app
    def replace_source(self, index, source):
        if not source:
            self.source_notice = 'VIDEO CHANGE CANCELLED / CURRENT SOURCE KEPT'
            return False
        previous = self.channels[index]
        previous.stop()
        self.retired.append(previous)
        self.channels[index] = Channel(index + 1, source, previous.show_ppe, previous.event_manager)
        self.channels[index].auto_low_score_rematch = previous.auto_low_score_rematch
        self.maps, self.tile_maps = {}, {}
        self.select(index)
        self.source_notice = 'VIDEO SOURCE CHANGED / OLD RESULTS CLEARED'
        return True

    def add_source(self, stream=False, *, source_allowed=None):
        try:
            from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog
            self.ensure_qt()
            labels = [f"Camera {i + 1}" for i in range(len(self.channels))]
            slot, ok = QInputDialog.getItem(None, "영상 위치", "영상을 넣을 화면을 선택하세요:", labels, self.selected, False)
            if not ok:
                self.source_notice = "VIDEO CHANGE CANCELLED / CURRENT SOURCE KEPT"
                return
            index = labels.index(slot)
            if stream:
                source, ok = QInputDialog.getText(None, "스트림 추가", "HTTPS YouTube 영상 주소:" if source_allowed is not None else "RTSP / HTTP / YouTube / gazebo:///.../image / gazebo-ssh://...:")
                if not ok:
                    self.source_notice = "VIDEO CHANGE CANCELLED / CURRENT SOURCE KEPT"
                    return
                source = source.strip()
                if source_allowed is not None and not source_allowed(source):
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.warning(None, "영상 주소 확인", "HTTPS YouTube 영상 주소를 입력해주세요.")
                    return
                if not source.startswith(("rtsp://", "rtsps://", "http://", "https://", "gazebo://", "gazebo-ssh://")):
                    print("RTSP / HTTP / YouTube / gazebo:///.../image / gazebo-ssh://...를 입력해주세요.")
                    return
            else:
                source, _ = QFileDialog.getOpenFileName(None, "영상 선택", str(ROOT / "videos"), "Video (*.mp4 *.mov *.avi *.mkv *.webm *.m4v);;All files (*)")
            if not source:
                self.source_notice = "VIDEO CHANGE CANCELLED / CURRENT SOURCE KEPT"
                return
            self.replace_source(index, source)
        except ImportError:
            print("영상 추가 창에 PySide6가 필요합니다: python -m pip install PySide6")
    def select_camera(self):
        """Manual source replacement; no automatic fallback or vehicle command."""
        from PySide6.QtWidgets import QInputDialog
        from tools.gazebo.oneclick import TOPIC
        self.ensure_qt()
        choices = {"DRONE CAM": "gazebo://" + TOPIC,
                   "FIXED CCTV": "gazebo:///safety/wildseed_forest_v3/observer/image"}
        previous = self.channels[self.selected]
        current = int('/observer/image' in str(previous.source))
        name, ok = QInputDialog.getItem(None, "Camera [C]", "수동 카메라 선택", list(choices), current, False)
        if not ok or previous.source == choices[name]:
            return
        previous.stop()
        self.retired.append(previous)
        replacement = Channel(self.selected + 1, choices[name], previous.show_ppe, previous.event_manager)
        replacement.auto_control.set_enabled(previous.auto_control.enabled)
        replacement.auto_result = replacement.auto_control.latest
        replacement.auto_low_score_rematch = previous.auto_low_score_rematch
        self.channels[self.selected] = replacement
        self.maps, self.tile_maps = {}, {}
        print(f"Camera: {name}; no automatic source switching")

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
        if self.telemetry.snapshot().get('telemetry_source') == 'PX4':
            print('[GPS] PX4 monitor uses actual MAVLink coordinates; manual SIM override disabled')
            return
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
        if self.telemetry.snapshot().get('telemetry_source') == 'PX4':
            print('[GPS] PX4 HOME is read-only and comes from HOME_POSITION')
            return
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
        x2 = getattr(self, "video_right", WIDTH) - 18
        y2 = HEIGHT - FOOTER - 12
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
        cx, cy, cw, ch = getattr(self, "map_crops", {}).get(index, (0, 0, fw, fh))
        point = tile_to_frame(x, y, (mx, my, mw, mh), (cx, cy, cw, ch), (fw, fh))
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
    def draw_status_panel(self, canvas, telem, mission, safe_command, validation, adapter_result):
        """Compact telemetry panel in 1 VIEW only; presentation does not control flight."""
        if self.layout != 1:
            return
        left = self.video_right
        right = WIDTH
        top = HEADER
        if right - left < 175:
            return
        bg, card = (28, 24, 19), (42, 36, 29)
        white, muted = (240, 245, 247), (165, 171, 180)
        green, red, yellow, cyan = (125, 222, 117), (105, 105, 255), (95, 210, 255), (245, 217, 120)
        cv2.rectangle(canvas, (left, top), (right - 1, HEIGHT - 1), bg, -1)
        cv2.line(canvas, (left, top), (left, HEIGHT), (85, 85, 90), 1)
        def txt(text, x, y, scale=0.43, color=white, thick=1):
            cv2.putText(canvas, str(text), (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX,
                        scale, color, thick, cv2.LINE_AA)
        x, end = left + 12, right - 12
        txt('DRONE STATUS', x, top + 25, 0.51, white, 2)
        online = bool(telem.get('telemetry_connected')) and float(telem.get('telemetry_age_s', 999)) <= 5
        source = str(telem.get('telemetry_source', 'SIM'))
        waiting = source in ('PX4', 'UDP') and not telem.get('telemetry_ever_connected', False)
        link_label = (f'{source} ONLINE' if online else
                      f'{source} CONNECTING' if waiting else f'{source} LOST')
        txt(link_label, x, top + 44, 0.38, green if online else yellow if waiting else red)
        cv2.line(canvas, (x, top + 55), (end, top + 55), (70, 70, 75), 1)

        # Tight line spacing; keep all fields within the 200-pixel column.
        y = top + 78
        def row(name, value, color=white):
            nonlocal y
            txt(name, x, y, 0.37, muted)
            txt(value, end - min(105, max(55, len(str(value)) * 7)), y, 0.44, color, 1)
            y += 27 if source == 'PX4' else 39
        battery = float(telem.get('battery', 0))
        bc = red if battery <= BATTERY_RTH else yellow if battery <= BATTERY_WARN else green
        row('BATTERY', f'{battery:.1f}%' if telem.get('battery_valid', True) else 'UNKNOWN',
            bc if telem.get('battery_valid', True) else yellow)
        mode = str(telem.get('flight_mode', 'UNKNOWN')).upper()
        mode_short = {'RETURNING_HOME': 'RETURNING', 'RTH_REQUESTED': 'RTH REQUEST', 'LANDED': 'LANDED', 'LAND': 'LANDING'}.get(mode, mode)
        if len(mode_short) > 12:
            mode_short = mode_short[:12]
        row('PX4 MODE' if source == 'PX4' else 'FLIGHT', mode_short, green if mode == 'LANDED' else yellow if mode in ('LAND', 'RETURNING_HOME', 'RTH_REQUESTED') else cyan)
        cv2.line(canvas, (x, y-15), (end, y-15), (70, 70, 75), 1)
        mission_name = str(mission.get('mission', 'UNKNOWN'))
        status = str(mission.get('status', 'UNKNOWN'))
        progress = max(0, min(100, int(mission.get('progress', 0))))
        complete = mission_name == 'RTH' and status == 'COMPLETED' and mode == 'LANDED'
        if source == "PX4":
            seq = telem.get("px4_mission_seq")
            total = telem.get("px4_mission_total")
            reached = telem.get("px4_mission_reached")
            px_state = str(telem.get("px4_mission_state", "UNKNOWN"))
            # Mission item completion and vehicle flight mode are separate facts.
            # Once MAVLink reports COMPLETE, later RTL heartbeats must not turn
            # the bar back from 100% to a seq-based percentage.
            items_done = bool(telem.get("px4_mission_items_complete")) or px_state == "COMPLETED"
            # While still flying the early mission items, a previous completion
            # must never paint the current mission as 100%.
            if mode in ("MISSION", "AUTO.MISSION") and total and seq is not None and seq < total - 1:
                items_done = False
            if online and total and total > 0:
                if items_done:
                    progress = 100
                elif seq is not None:
                    # Approximate item index only, not distance/flight completion.
                    progress = min(99, max(0, int(100 * min(seq, total) / total)))
                else:
                    progress = 0
                display_seq = min(max(0, int(seq) + 1), total) if seq is not None else 0
                short_status = f"PX4 {px_state} {display_seq}/{total}"
            else:
                progress = 0
                short_status = f"PX4 {px_state} --/--"
            # Color signifies completed mission items, not landed vehicle.
            complete = items_done
            mission_name = "PX4"
        txt('PX4 MISSION' if source == "PX4" else 'RTH / MISSION', x, y + 1, 0.39, muted)
        progress_label = '--' if source == 'PX4' and not (online and telem.get('px4_mission_total')) else f'{progress}%'
        txt(progress_label, end - 47, y + 1, 0.49, green if complete else cyan, 2)
        y += 19
        if source != 'PX4':
            short_status = 'RTH COMPLETE' if complete else f'{mission_name}:{status}'
        txt(short_status[:22], x, y, 0.39, green if complete else white)
        y += 12
        cv2.rectangle(canvas, (x, y), (end, y+6), (65, 66, 70), -1)
        if progress:
            cv2.rectangle(canvas, (x, y), (x + round((end-x)*progress/100), y+6), green if complete else cyan, -1)
        y += 38
        coords = [telem.get(k) for k in ('lat','lon','home_lat','home_lon')]
        if all(v is not None for v in coords):
            lat, lon, home_lat, home_lon = map(float, coords)
            dist = (((lat-home_lat)*111320)**2 + ((lon-home_lon)*111320*np.cos(np.radians(home_lat)))**2)**0.5
            row('HOME', f'{dist:.1f}m', green)
        else:
            row('HOME', 'NO FIX' if source == 'PX4' and not telem.get('gps_valid') else 'NOT SET', yellow)
        altitude = telem.get('altitude_m')
        row('ALT REL' if source == 'PX4' else 'ALT', f'{float(altitude):.1f}m' if altitude is not None else '--')
        if source == 'PX4':
            msl = telem.get('global_alt_msl_m')
            row('ALT MSL', f'{float(msl):.1f}m' if msl is not None else '--')
            row('GPS FIX', f"{telem.get('gps_fix_type','--')} {telem.get('gps_status','UNKNOWN')}", green if telem.get('gps_valid') else yellow)
            row('AIR', telem.get('landed_state','UNKNOWN'), cyan)
        heading = telem.get('heading_deg')
        row('HDG', f'{float(heading):.0f}deg' if heading is not None else '--')
        row('AGENT' if source == 'PX4' else 'CMD', str(safe_command.get('command', '--'))[:12], cyan)
        if str(telem.get("telemetry_source")) == "PX4":
            armed = telem.get("armed")
            txt("ARMED: " + ("YES" if armed else "NO" if armed is False else "--"), x, y + 1, 0.40, yellow if armed else muted)
            y += 24
            txt("READ ONLY - NO FLIGHT COMMANDS", x, y + 1, 0.29, yellow)
            y += 16
            sid, cid = telem.get('px4_system_id') or '--', telem.get('px4_component_id') or '--'
            age = (telem.get('px4_message_ages_s') or {}).get('HEARTBEAT')
            txt(f"SYS {sid} / CMP {cid} | HB " + (f"{age:.1f}s" if age is not None else '--'),x,y,.28,muted)
            y += 16
            lat,lon=telem.get('reported_lat'),telem.get('reported_lon')
            txt(f"GPI {lat:.7f},{lon:.7f}" if lat is not None and lon is not None else 'GPI POS --',x,y,.28,muted)
            y += 16
            stamp=telem.get('last_mavlink_wall_unix')
            src=self.channels[self.selected].source
            camera='FIXED CCTV [C]' if '/observer/image' in src else 'DRONE CAM [C]' if src.startswith(('gazebo://','gazebo-ssh://')) else 'OTHER'
            txt('CAM '+camera+' | RX '+(time.strftime('%H:%M:%S',time.localtime(stamp)) if stamp else '--'),x,y,.27,muted)
        else:
            txt('ADAPTER: ' + str(adapter_result.get('adapter','--'))[:8], x, y + 2, 0.37, muted)

        # Existing dashboard layout and typography remain unchanged.
        risk = self.current_risk
        level = risk.get("level", "NONE")
        level_color = red if level == "HIGH" else yellow if level == "MEDIUM" else cyan if level == "LOW" else green
        risk_y = max(y + 38, top + 510)
        if risk_y + 50 < HEIGHT - FOOTER:
            cv2.line(canvas, (x, risk_y - 15), (end, risk_y - 15), (70, 70, 75), 1)
            txt("RISK", x, risk_y + 2, 0.40, muted)
            txt(level, end - 72, risk_y + 2, 0.47, level_color, 1)
            reason = ",".join(risk.get("reasons", [])) or "NONE"
            txt(reason[:24], x, risk_y + 22, 0.33, level_color)
            txt(risk.get("advice", "NORMAL")[:24], x, risk_y + 40, 0.32, muted)

    def draw(self):
        global WIDTH, HEIGHT
        if self.main_view is not None:
            WIDTH, HEIGHT = self.main_view.canvas_size()  # 실제 창 크기로 매번 배치 계산
        canvas = np.full((HEIGHT, WIDTH, 3), 22, np.uint8)
        self.zoom()
        view_left = 0  # 안내판 없이 영상이 창 전체 너비를 사용
        view_width = max(1, WIDTH - (200 if self.layout == 1 else 0) - view_left)
        self.video_right = view_left + view_width
        c = self.channels[self.selected]
        self.io_bridge.poll_telemetry()
        self.telemetry.sim_heartbeat()
        self.safety.update(c)
        self.agent_state.update(self.channels)
        agent = self.agent_state.snapshot()
        self.current_risk = self.risk_engine.evaluate(agent)
        decision = self.agent_engine.decide(agent)
        command = self.command_builder.build(decision, agent)
        validation = self.command_validator.validate(command, agent)
        safe_command = validation["safe_command"]
        adapter_result = self.io_bridge.send_command(safe_command, agent["drone"])
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
        draw_top_menu_text(canvas, full_text, (WIDTH - 136, 58), 0.5)
        for index, name in enumerate(TOOLS):
            left = 10 + index * 105
            color = (0, 150, 0) if c.mode == name else (65, 65, 65)
            cv2.rectangle(canvas, (left, 5), (left + 100, 35), color, -1)
            draw_top_menu_text(canvas, name, (left + 8, 27), 0.55)
        for index, layout in enumerate((1, 2, 4)):
            left = 570 + index * 90
            cv2.rectangle(canvas, (left, 5), (left + 82, 35), (120, 90, 0) if self.layout == layout else (65, 65, 65), -1)
            draw_top_menu_text(canvas, f"{layout} VIEW", (left + 7, 27), 0.48)
        cv2.rectangle(canvas, (840, 5), (945, 35), (120, 90, 0) if self.layout not in (1, 2, 4) else (65, 65, 65), -1)
        draw_top_menu_text(canvas, f"VIEW:{self.layout}", (850, 27), 0.52)
        gps_text = "GPS:--" if telem["lat"] is None or telem["lon"] is None else f"GPS:{telem['lat']:.5f},{telem['lon']:.5f}"
        home_text = "HOME:--" if telem["home_lat"] is None or telem["home_lon"] is None else f"HOME:{telem['home_lat']:.5f},{telem['home_lon']:.5f}"

        battery = telem["battery"]
        battery_text = "UNKNOWN" if battery is None or not telem.get("battery_valid", True) else f"{battery:.1f}%"
        if battery_text == "UNKNOWN":
            battery_state = "UNKNOWN"
        elif battery <= BATTERY_RTH:
            battery_state = "RTH"
        elif battery <= BATTERY_LIMIT:
            battery_state = "LIMIT"
        elif battery <= BATTERY_WARN:
            battery_state = "WARN"
        else:
            battery_state = "OK"

        fire_state = "ON" if c.show_fire_smoke else "OFF"
        tel_source = str(telem["telemetry_source"])
        tel_fresh = bool(telem["telemetry_connected"]) and float(telem.get("telemetry_age_s", 999)) <= 5.0
        tel_state = (tel_source if tel_fresh else
                     f"{tel_source}:WAIT" if tel_source in ("PX4", "UDP") and not telem.get("telemetry_ever_connected", False)
                     else f"{tel_source}:LOST")
        profile_key = ("PX4-SITL-X500" if telem.get("telemetry_source") == "PX4" else
                       telem["drone_profile"]["key"].replace("SKYDIO_", "").replace("_REF", ""))
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
        gps_short = ("GPS:" + str(telem.get('gps_status', 'NO FIX')) if tel_source == 'PX4'
                     else "GPS:SET" if telem["lat"] is not None and telem["lon"] is not None else "GPS:--")
        home_short = "HOME:SET" if telem["home_lat"] is not None and telem["home_lon"] is not None else "HOME:--"
        cv2.putText(
            canvas,
            f"Cam:{c.number} | Tool:{c.mode} | SITE:{c.site_mode} | FIRE:{fire_state} | "
            f"BAT:{battery_text}[{battery_state}] | {gps_short} | {home_short} | "
            f"ALT:{alt_text} | HDG:{hdg_text} | TEL:{tel_state} | X10:{profile_key} | {telem['flight_mode']}",
            (10, 58), cv2.FONT_HERSHEY_DUPLEX, 0.38, (0, 255, 255), 1
        )
        if battery_state in ("WARN", "LIMIT", "RTH"):
            warning_text = {
                "WARN": f"BATTERY LOW: {battery:.1f}% (WARN)",
                "LIMIT": f"BATTERY LOW: {battery:.1f}% (MISSION LIMIT)",
                "RTH": f"BATTERY CRITICAL: {battery:.1f}% (RTH REQUEST)",
            }[battery_state]
            cv2.putText(
                canvas,
                warning_text,
                (WIDTH - 430, 58),
                cv2.FONT_HERSHEY_DUPLEX,
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
                cv2.FONT_HERSHEY_DUPLEX,
                0.55,
                (0, 0, 255) if flight_mode != "LAND" else (0, 255, 255),
                2,
            )

        for text, left, right in (("ADD FILE [O]", 950, 1095), ("ADD URL [U]", 1105, 1270)):
            cv2.rectangle(canvas, (left, 5), (right, 35), (100, 85, 25), -1)
            draw_top_menu_text(canvas, text, (left + 8, 27), 0.5)
        visible = [self.selected] if self.layout == 1 else list(range(self.layout))
        cols, rows = self.grid_shape(self.layout)
        cell_w, cell_h = view_width // cols, (HEIGHT - HEADER - FOOTER) // rows
        self.maps = {}
        self.map_crops = {}
        self.tile_maps = {}
        for position, index in enumerate(visible):
            left, top = view_left + (position % cols) * cell_w, HEADER + (position // cols) * cell_h
            self.tile_maps[index] = (left, top, cell_w, cell_h)
            if index >= len(self.channels):
                cv2.putText(canvas, f"Camera {index + 1}: No source", (left + 20, top + 40), cv2.FONT_HERSHEY_DUPLEX, 0.7, (150, 150, 150), 1)
                continue
            channel = self.channels[index]
            raw, image, objects = live_snapshot(channel)
            with channel.lock:
                display_now = time.monotonic()
                shown_sequence = getattr(channel, 'presentation_sequence', channel.sequence)
                if raw is not None and shown_sequence != channel.display_sequence:
                    channel.display_rate.mark(display_now)
                    channel.display_sequence = shown_sequence
                channel.display_fps = channel.display_rate.fps(display_now)
                danger, roi, selected = channel.danger, channel.roi, channel.selected_id
                receiving = display_now - channel.received_at < 2
                receive_fps = channel.receive_fps if receiving else 0.0
                age = display_now - channel.overlay_time
                ai_fps = channel.analysis_rate.fps(display_now) if age < 2 else 0.0
                overlay_age_ms = ((display_now - channel.analysis_received_at) * 1000
                                  if channel.analysis_received_at and age <= 0.7 else None)
                auto_result = channel.auto_result
                auto_enabled = channel.auto_control.enabled
                video_received_at = channel.received_at
                auto_fresh = (auto_enabled and receiving and age <= 0.7
                              and channel.sequence - channel.analysis_sequence <= 1)
                people, vehicles = (channel.people, channel.vehicles) if objects else (0, 0)
                lag = f"{overlay_age_ms:.0f}ms" if overlay_age_ms is not None else "--"
                stats = (f"Cam {channel.number} | P:{people} V:{vehicles} | RX:{receive_fps:.1f} "
                         f"AI:{ai_fps:.1f} DISP:{channel.display_fps:.1f} AGE:{lag} | {channel.status}")
                if self.show_auto_debug:
                    stats += f" AUTO:{auto_result['state'] if auto_enabled else 'OFF'}"
            if image is None:
                image = np.full((360, 640, 3), 25, np.uint8)
                cv2.putText(image, f"Camera {channel.number}: {channel.status}", (20, 180), cv2.FONT_HERSHEY_DUPLEX, 0.7, (200, 200, 200), 1)
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
                if auto_enabled:
                    if auto_fresh and auto_result.get("target") is not None:
                        px, py = map(round, auto_result["target"])
                        cv2.drawMarker(image, (px, py), (255, 255, 0), cv2.MARKER_CROSS, 20, 1)
                    if self.show_auto_debug:
                        auto_state = auto_result["state"] if auto_fresh else "STALE"
                        label = f"AUTO {auto_state} | VIRTUAL ONLY"
                        crowd_level = (auto_result.get("crowd") or {}).get("level", "NORMAL") if auto_fresh else "--"
                        if auto_fresh and auto_result.get("target") is not None:
                            label += f" | {auto_result['target_kind']}:{auto_result['target_confidence']:.2f} ({px},{py})"
                        cv2.putText(image, label, (8, image.shape[0] - 30),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 0), 1, cv2.LINE_AA)
                        origin = auto_result.get("target_source") or "NONE"
                        cv2.putText(image, f"CROWD {crowd_level} | IMAGE-SPACE / UNCALIBRATED | {origin}",
                                    (8, image.shape[0] - 12),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 0), 1, cv2.LINE_AA)
            video_height = cell_h if globals().get("SUPPRESS_INLINE_STATS", False) else cell_h - 28
            tile, (ox, oy, nw, nh), source_crop = fit_image(image, cell_w, video_height,
                fill=getattr(self, "display_fill", False))
            crop, caption = target_crop(channel, raw, objects)
            roi_status = ""
            if crop is None and auto_enabled:
                panel = channel.auto_roi_panel.snapshot(display_now, video_received_at)
                if panel is not None:
                    crop = panel["crop"] if panel["crop"] is not None else np.full((240, 320, 3), 25, np.uint8)
                    caption = (f"DIGITAL ROI {panel['kind']}" if panel["crop"] is not None
                               else "DIGITAL ROI WAITING")
                    roi_status = panel["status"]
            if crop is not None and nw > 32 and nh > 32:
                inset_scale = min(1.0, (nw - 16) / 320, (nh - 16) / 240)
                inset_w, inset_h = max(1, round(320 * inset_scale)), max(1, round(240 * inset_scale))
                inset = cv2.resize(crop, (inset_w, inset_h), interpolation=cv2.INTER_CUBIC)  # 원래 320×240 미리보기 방식
                blurred = cv2.GaussianBlur(inset, (0, 0), 1.0)
                inset = cv2.addWeighted(inset, 1.5, blurred, -0.5, 0)
                cv2.putText(inset, caption, (8, 24), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 255, 255), 1)
                if roi_status and roi_status != "LIVE":
                    cv2.putText(inset, roi_status, (8, 48), cv2.FONT_HERSHEY_DUPLEX,
                                0.45, (0, 255, 255), 1, cv2.LINE_AA)
                ix, iy = ox + nw - inset_w - 8, oy + nh - inset_h - 8
                tile[iy:iy + inset_h, ix:ix + inset_w] = inset
                cv2.rectangle(tile, (ix, iy), (ix + inset_w - 1, iy + inset_h - 1), (0, 255, 255), 2)
            if raw is not None and channel.show_direction_overlay:
                draw_compass(tile, channel.smoke_motion_estimator.snapshot(display_now), cv2,
                             side='left' if crop is not None else 'right', compact=globals().get('COMPACT_COMPASS', False))
            canvas[top:top + video_height, left:left + cell_w] = tile
            if raw is not None:
                fh, fw = raw.shape[:2]
                self.maps[index] = left + ox, top + oy, nw, nh, fw, fh
                self.map_crops[index] = source_crop
            if not globals().get("SUPPRESS_INLINE_STATS", False):
                cv2.putText(canvas, stats, (left + 6, top + cell_h - 9), cv2.FONT_HERSHEY_DUPLEX, 0.42, (235, 235, 235), 1)
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
            f"ALT:{alt_text} | HDG:{hdg_text} | BAT:{battery_text}[{battery_state}] | "
            f"TEL:{tel_state} | DRONE:{profile_key} | FLIGHT:{telem['flight_mode']} | "
            f"{smoke_status_line(self.channels[self.selected].smoke_motion_estimator.snapshot(time.monotonic()), ascii_only=True)}"
        )

        # 창 너비에 맞춰 하단 상태 정보를 자동 줄바꿈한다.
        # 긴 PX4/Agent/Telemetry 텍스트를 잘라내지 않고 모든 정보를 보존한다.
        def wrap_status(text, scale, max_width):
            pieces = text.split(" | ")
            lines = []
            current = ""
            font = cv2.FONT_HERSHEY_DUPLEX
            for piece in pieces:
                candidate = f"{current} | {piece}" if current else piece
                if cv2.getTextSize(candidate, font, scale, 1)[0][0] <= max_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    current = ""
                # Extremely long single field: wrap on characters instead of clipping.
                for char in piece:
                    candidate = current + char
                    if current and cv2.getTextSize(candidate, font, scale, 1)[0][0] > max_width:
                        lines.append(current)
                        current = char
                    else:
                        current = candidate
            if current:
                lines.append(current)
            return lines

        max_text_width = max(100, WIDTH - 22)
        agent_rows = wrap_status(agent_line, 0.42, max_text_width)
        telemetry_rows = wrap_status(telemetry_line, 0.39, max_text_width)
        text_rows = ([(line, 0.42, (0, 255, 255)) for line in agent_rows]
                     + [(line, 0.39, (220, 220, 220)) for line in telemetry_rows])
        # The footer occupies 75 px by default. Fit any additional rows inside
        # the available area without dropping telemetry fields.
        line_height = min(20, max(11, (FOOTER - 12) // max(1, len(text_rows))))
        for i, (line, scale, color) in enumerate(text_rows):
            cv2.putText(canvas, line, (10, footer_top + 17 + i * line_height),
                        cv2.FONT_HERSHEY_DUPLEX, scale, color, 1, cv2.LINE_AA)

        if self.analyzer.error:
            cv2.putText(
                canvas,
                "AI ERROR - See terminal",
                (max(10, self.video_right - 210), footer_top + 43),
                cv2.FONT_HERSHEY_DUPLEX,
                0.40,
                (0, 0, 255),
                1,
            )

        event_manager = c.event_manager if c is not None else None
        self.draw_gps_map(canvas, telem, event_manager)
        self.draw_status_panel(canvas, telem, mission, safe_command, validation, adapter_result)

        if self.main_view is None:
            self.ensure_qt()
            self.main_view = MainView(self)
        self.main_view.update(canvas)
        if self.dialog_app is not None:
            self.dialog_app.processEvents()  # 별도 확대창 이동·크기 조절·닫기 처리

def main():
    parser = argparse.ArgumentParser(description="드론 안전 관제 트래커: 최대 16개 카메라 슬롯")
    parser.add_argument("sources", nargs="*", help="영상 파일, RTSP/HTTP/YouTube, gazebo:///safety/probe/image, gazebo-ssh://USER@VM_IP/TOPIC?... 또는 카메라 번호 (최대 16개)")
    parser.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--no-ppe", action="store_true", help="PPE 검사 끄기 (원거리 영상용, P키로 개별 전환)")
    parser.add_argument("--auto-track", action="store_true",
                        help="가상 조준·디지털 ROI 재확인 실험 시작 (기본 OFF, T키 전환; 기체 명령 없음)")
    parser.add_argument("--person-rematch-experiment", action="store_true",
                        help="저신뢰 사람 후보 ByteTrack 재매칭 실험 (기본 추적 설정과 별개)")
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
        choices=("SIM", "UDP", "PX4", "ROS2", "SKYDIO"),
        default="SIM",
        help="비행 명령 출력 어댑터. 실제 기체 미연결 시 SIM 사용",
    )
    parser.add_argument("--px4-system-id", type=int, default=1, help="Expected PX4 vehicle SYSTEM_ID")
    parser.add_argument("--px4-component-id", type=int, default=1, help="Expected autopilot COMPONENT_ID")
    parser.add_argument("--px4-telemetry-port", type=int, default=14540,
                        help="PX4 SITL MAVLink 수신 포트 (localhost, 기본 14540, 수신 전용)")
    parser.add_argument(
        "--udp-host",
        default="127.0.0.1",
        help="UDP 명령 전송 대상 호스트 (기본 localhost)",
    )
    parser.add_argument(
        "--udp-telemetry-port",
        type=int,
        default=14560,
        help="UDP 텔레메트리 수신 포트",
    )
    parser.add_argument(
        "--udp-command-port",
        type=int,
        default=14561,
        help="UDP 명령 전송 포트",
    )
    parser.add_argument(
        "--smoke-camera-yaw-offset", type=float, default=None,
        help="이벤트 로그용 연기 추정 방위 보정값(도). 화면의 연기 나침반은 항상 화면 기준으로 표시",
    )
    args = parser.parse_args()
    if args.auto_track and args.flight_adapter not in ("SIM", "PX4"):
        parser.error("--auto-track is limited to SIM or read-only PX4 adapters")
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
    # UDP mode must not pretend SIM telemetry is connected before the first packet.
    if args.flight_adapter in ("UDP", "PX4"):
        with telemetry.lock:
            telemetry.telemetry_source = args.flight_adapter
            telemetry.telemetry_connected = False
            telemetry.telemetry_ever_connected = False
            telemetry.telemetry_last_update = time.monotonic() - 60.0
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
    for channel in channels[:len(sources)]:
        channel.auto_control.set_enabled(args.auto_track)
        channel.auto_result = channel.auto_control.latest
        channel.auto_low_score_rematch = args.person_rematch_experiment
    analyzer = Analyzer(channels, args.device)
    analyzer.smoke_camera_yaw_offset = args.smoke_camera_yaw_offset
    if args.smoke_camera_yaw_offset is not None:
        print(f"[SMOKE BEARING] Heading estimate ON | camera yaw offset={args.smoke_camera_yaw_offset:.1f} deg | CALIBRATION REQUIRED, NOT WIND")
    else:
        print("[SMOKE BEARING] SCREEN ONLY | enable heading estimate using --smoke-camera-yaw-offset (calibrated camera only)")
    safety = SafetyController(telemetry, event_manager)
    agent_state = AgentState(telemetry)
    agent_engine = AgentDecisionEngine()
    command_builder = DroneCommandBuilder()
    command_validator = CommandSafetyValidator()
    flight_adapter = create_flight_adapter(
        args.flight_adapter,
        udp_host=args.udp_host,
        udp_command_port=args.udp_command_port,
    )
    io_bridge = DroneIOBridge(
        telemetry_input,
        flight_adapter,
        udp_telemetry_port=args.udp_telemetry_port,
    )
    px4_receiver = None
    if args.flight_adapter == "PX4":
        px4_receiver = Px4TelemetryReceiver(telemetry_input, port=args.px4_telemetry_port,
                                           system_id=args.px4_system_id, component_id=args.px4_component_id)
        px4_receiver.start()
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
    elif args.flight_adapter == "UDP":
        print(
            f"[FLIGHT ADAPTER] UDP -> READY | "
            f"telemetry=127.0.0.1:{args.udp_telemetry_port} | "
            f"commands={args.udp_host}:{args.udp_command_port}"
        )
    else:
        print(
            f"[FLIGHT ADAPTER] {args.flight_adapter} -> "
            + ("READ-ONLY MAVLink telemetry / NO COMMANDS" if args.flight_adapter == "PX4"
               else "STUB (interface ready / real connection not implemented)")
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
    audit_at, audit_paints = time.monotonic(), 0
    try:
        while True:
            dashboard.draw()
            if event_manager.safety_evidence is not None and time.monotonic()-audit_at >= 1:
                from tools.ai.safety_evidence import runtime_snapshot
                now = time.monotonic()
                event_manager.safety_evidence.runtime(runtime_snapshot(
                    channels, telemetry.snapshot(), dashboard.main_view.paint_count,
                    audit_paints, now-audit_at, now))
                audit_at, audit_paints = now, dashboard.main_view.paint_count
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
            elif key == ord("c"):
                dashboard.select_camera()
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
                    c.smoke_motion_estimator = SmokeMotion()
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
            elif key == ord("t"):
                c = channels[dashboard.selected]
                if io_bridge.input_source not in ("SIM", "PX4"):
                    print("[AUTO] Virtual tracking requires SIM or read-only PX4 adapter")
                    continue
                with c.auto_lock:
                    c.auto_control.set_enabled(not c.auto_control.enabled)
                    with c.lock:
                        c.auto_result = c.auto_control.latest
                    if not c.auto_control.enabled:
                        c.auto_roi_panel.reset()
                print(f"[AUTO] Camera {c.number} -> {'ON' if c.auto_control.enabled else 'OFF'} | VIRTUAL ONLY")
            elif key == ord("l"):
                c = channels[dashboard.selected]
                c.show_direction_overlay = not c.show_direction_overlay
                print(f"[DIRECTION] Camera {c.number} -> {'ON' if c.show_direction_overlay else 'OFF'}")
            elif key == ord("i"):
                dashboard.show_auto_debug = not dashboard.show_auto_debug
                print(f"[AUTO DEBUG] {'ON' if dashboard.show_auto_debug else 'OFF'}")
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
        if px4_receiver is not None:
            px4_receiver.stop_event.set()
            px4_receiver.join(timeout=2.0)
        io_bridge.close()

if __name__ == "__main__":
    main()
