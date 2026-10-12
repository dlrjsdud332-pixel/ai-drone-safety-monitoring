from collections import deque
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
WIDTH, HEIGHT, HEADER, FOOTER = 1440, 810, 70, 32
TOOLS = ("SELECT", "ZOOM", "MOVE", "RESET", "DANGER")
VEHICLES = (2, 3, 5, 7)

# ===== 추가 안전관제 설정 =====
EVENT_PRE_SECONDS = 5
EVENT_POST_SECONDS = 10
EVENT_CLIP_FPS = 5
EVENT_COOLDOWN = 30
FIRE_CONFIRM_HITS = 4
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

class TelemetryState:
    """PX4/ROS2 연동 전까지 사용하는 공통 드론 상태 저장소."""
    def __init__(self, battery=100.0, lat=None, lon=None, home_lat=None, home_lon=None):
        self.lock = threading.Lock()
        self.battery = float(battery)
        self.lat = lat
        self.lon = lon
        self.home_lat = home_lat if home_lat is not None else lat
        self.home_lon = home_lon if home_lon is not None else lon
        self.flight_mode = "PATROL"
        self.rth_requested = False

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
            }

    def request_rth(self):
        with self.lock:
            if not self.rth_requested:
                self.rth_requested = True
                self.flight_mode = "RTH_REQUESTED"
                return True
            return False

class EventManager:
    """탐지 결과를 FIRE/SMOKE/CROWD/FALL/INTRUSION/PPE 공통 이벤트로 기록."""
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.last_event = {}
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

class SafetyController:
    """Agent 이전 단계의 최소 안전 규칙. 실제 비행 명령은 아직 보내지 않음."""
    def __init__(self, telemetry, event_manager):
        self.telemetry = telemetry
        self.event_manager = event_manager
        self.last_level = None

    def update(self, channel):
        state = self.telemetry.snapshot()
        battery = state["battery"]
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
                details={"battery": battery, "action": "RTH_REQUESTED"},
            )

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
        self.fire_hits = self.smoke_hits = self.crowd_hits = 0
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
            c.pose_result = pose.predict(frame, conf=0.50, imgsz=640, device=self.device, verbose=False)[0]
            c.ppe_result = ppe.predict(frame, conf=0.40, imgsz=640, classes=[0, 1, 2, 4], device=self.device, verbose=False)[0] if c.show_ppe else None
            c.fire_result = fire_smoke.predict(frame, conf=0.50, imgsz=640, device=self.device, verbose=False)[0] if fire_smoke is not None else None
        if self.device == "mps":
            torch.mps.synchronize()  # 모든 모델 추론은 한 스레드에서 순서대로 실행
        image = frame.copy()
        now = time.time()

        # FIRE / SMOKE: 단발 오탐을 줄이기 위해 일정 횟수 연속 검출 시 이벤트 확정
        if inspect and c.fire_result is not None:
            fire_conf = smoke_conf = 0.0
            for box in c.fire_result.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                name = str(c.fire_result.names.get(cls_id, cls_id)).lower().replace(" ", "_")
                if "fire" in name or "flame" in name:
                    fire_conf = max(fire_conf, conf)
                if "smoke" in name:
                    smoke_conf = max(smoke_conf, conf)

            c.fire_hits = c.fire_hits + 1 if fire_conf >= 0.50 else max(0, c.fire_hits - 1)
            c.smoke_hits = c.smoke_hits + 1 if smoke_conf >= 0.50 else max(0, c.smoke_hits - 1)

            if c.fire_hits >= FIRE_CONFIRM_HITS:
                c.fire_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(c, "FIRE", fire_conf, details={"confirmed_hits": c.fire_hits})
                c.fire_hits = 0
            if c.smoke_hits >= SMOKE_CONFIRM_HITS:
                c.smoke_until = now + 10
                if c.event_manager:
                    c.event_manager.emit(c, "SMOKE", smoke_conf, details={"confirmed_hits": c.smoke_hits})
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
        recent = time.monotonic() - channel.overlay_time <= 0.7
    if raw is None:
        return None, None, {}
    display = raw.copy()  # AI 완료를 기다리지 않고 최신 수신 영상을 표시
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
    if code in [ord(c) for c in "1234VSZMDRPOUQFK"]:
        return code + 32 if 65 <= code <= 90 else code
    text = event.text().lower()
    if len(text) == 1 and text in "1234vszmdrpouqfk":
        return ord(text)
    if sys.platform == "darwin":
        physical = {18: "1", 19: "2", 20: "3", 21: "4", 9: "v", 1: "s", 6: "z", 46: "m", 2: "d", 15: "r", 35: "p", 31: "o", 32: "u", 12: "q", 3: "f", 40: "k", 53: "\x1b"}
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
                elif code in [ord(c) for c in "1234VSZMDRPOUQFK"]:
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
    def __init__(self, channels, analyzer, telemetry, safety):
        self.channels, self.analyzer = channels, analyzer
        self.telemetry, self.safety = telemetry, safety
        self.selected, self.layout = 0, 1
        self.maps, self.drag_channel = {}, None
        self.dialog_app, self.retired = None, []
        self.floating_zoom = None
        self.main_view = None
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
            labels = [f"Camera {i + 1}" for i in range(4)]
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
            self.select(index)
            print(f"Camera {index + 1}: 영상 변경 완료")
        except ImportError:
            print("영상 추가 창에 PySide6가 필요합니다: python -m pip install PySide6")
    def set_layout(self, layout):
        self.layout, self.drag_channel = layout, None
        for c in self.channels:
            c.drag_start = c.drag_end = None
    def select(self, index):
        if index >= len(self.channels):
            return
        self.selected, self.drag_channel = index, None
        if self.layout == 2 and index >= 2:
            self.set_layout(4)  # 3/4번 선택 시 선택한 영상이 화면에 보이도록 전환
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
                if 570 + index * 115 <= x < 680 + index * 115 and y < 36:
                    self.set_layout(layout)
                    return
            return
        index = self.drag_channel if self.drag_channel is not None else next((i for i, rect in self.maps.items() if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]), None)
        if index is None or index not in self.maps:
            return
        c = self.channels[index]
        mx, my, mw, mh, fw, fh = self.maps[index]
        point = (int(np.clip((x - mx) * fw / mw, 0, fw - 1)), int(np.clip((y - my) * fh / mh, 0, fh - 1)))
        if event == cv2.EVENT_LBUTTONDBLCLK:
            self.select(index)
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
        self.safety.update(c)
        telem = self.telemetry.snapshot()
        cv2.rectangle(canvas, (WIDTH - 145, 40), (WIDTH - 10, 65), (95, 75, 30), -1)
        full_text = "WINDOW [F]" if self.main_view is not None and self.main_view.is_fullscreen() else "FULL [F]"
        cv2.putText(canvas, full_text, (WIDTH - 136, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, getattr(cv2, "LINE_AA", 16))
        for index, name in enumerate(TOOLS):
            left = 10 + index * 105
            color = (0, 150, 0) if c.mode == name else (65, 65, 65)
            cv2.rectangle(canvas, (left, 5), (left + 100, 35), color, -1)
            cv2.putText(canvas, name, (left + 8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        for index, layout in enumerate((1, 2, 4)):
            left = 570 + index * 115
            cv2.rectangle(canvas, (left, 5), (left + 110, 35), (120, 90, 0) if self.layout == layout else (65, 65, 65), -1)
            cv2.putText(canvas, f"{layout} VIEW", (left + 10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        gps_text = "GPS:--" if telem["lat"] is None or telem["lon"] is None else f"GPS:{telem['lat']:.5f},{telem['lon']:.5f}"
        cv2.putText(
            canvas,
            f"Selected: Camera {c.number} | Tool:{c.mode} | SITE:{c.site_mode} | BAT:{telem['battery']:.1f}% | {gps_text} | {telem['flight_mode']}",
            (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 255), 1
        )
        for text, left, right in (("ADD FILE [O]", 950, 1095), ("ADD URL [U]", 1105, 1270)):
            cv2.rectangle(canvas, (left, 5), (right, 35), (100, 85, 25), -1)
            cv2.putText(canvas, text, (left + 8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        visible = [self.selected] if self.layout == 1 else list(range(self.layout))
        cols, rows = (1, 1) if self.layout == 1 else (2, 1) if self.layout == 2 else (2, 2)
        cell_w, cell_h = view_width // cols, (HEIGHT - HEADER - FOOTER) // rows
        self.maps = {}
        for position, index in enumerate(visible):
            left, top = view_left + (position % cols) * cell_w, HEADER + (position // cols) * cell_h
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
                for rect, color in ((danger, (0, 0, 255)), (roi, (255, 255, 0))):
                    if rect is not None:
                        cv2.rectangle(image, rect[:2], rect[2:], color, 1)
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
        message = ""  # 단축키 목록은 화면에서 숨겨 깔끔하게 유지
        if self.analyzer.error:
            message = "AI ERROR - See terminal: " + self.analyzer.error[:100]
        cv2.putText(canvas, message, (10, HEIGHT - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        if self.main_view is None:
            self.ensure_qt()
            self.main_view = MainView(self)
        self.main_view.update(canvas)
        if self.dialog_app is not None:
            self.dialog_app.processEvents()  # 별도 확대창 이동·크기 조절·닫기 처리

def main():
    parser = argparse.ArgumentParser(description="기존 드론 트래커: 최대 4개 영상 독립 추적")
    parser.add_argument("sources", nargs="*", help="영상 파일, RTSP/HTTP/YouTube 주소 또는 카메라 번호 (최대 4개)")
    parser.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--no-ppe", action="store_true", help="PPE 검사 끄기 (원거리 영상용, P키로 개별 전환)")
    parser.add_argument("--battery", type=float, default=100.0, help="PX4 연동 전 테스트용 배터리 퍼센트")
    parser.add_argument("--lat", type=float, default=None, help="PX4 연동 전 테스트용 현재 위도")
    parser.add_argument("--lon", type=float, default=None, help="PX4 연동 전 테스트용 현재 경도")
    parser.add_argument("--home-lat", type=float, default=None, help="테스트용 홈 위도")
    parser.add_argument("--home-lon", type=float, default=None, help="테스트용 홈 경도")
    args = parser.parse_args()
    sources = args.sources or ["https://www.youtube.com/watch?v=C3hW1VrwmNc"]
    if not 1 <= len(sources) <= 4:
        parser.error("영상은 1~4개까지 지정해주세요.")
    needed = ["yolo11s.pt", "yolo11n-pose.pt", "ppe_best.pt"]
    for name in needed:
        if not (ROOT / "models" / name).is_file():
            parser.error(f"모델 파일 없음: {ROOT / 'models' / name}")
    telemetry = TelemetryState(args.battery, args.lat, args.lon, args.home_lat, args.home_lon)
    event_manager = EventManager(telemetry)
    print(
        f"[POLICY] context filter ON | detector conf=0.35 | "
        f"event cooldown={EVENT_COOLDOWN}s | retention={EVENT_RETENTION_DAYS}d | "
        f"max events/camera={MAX_EVENT_BUNDLES_PER_CAMERA}"
    )
    channels = [
        Channel(i + 1, sources[i] if i < len(sources) else "", not args.no_ppe, event_manager)
        for i in range(4)
    ]
    analyzer = Analyzer(channels, args.device)
    safety = SafetyController(telemetry, event_manager)
    dashboard = Dashboard(channels, analyzer, telemetry, safety)
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
                dashboard.set_layout({1: 2, 2: 4, 4: 1}[dashboard.layout])
            elif key in map(ord, "szmdr"):
                name = dict(zip(map(ord, "szmdr"), ("SELECT", "ZOOM", "MOVE", "DANGER", "RESET")))[key]
                dashboard.mouse(cv2.EVENT_LBUTTONDOWN, 15 + TOOLS.index(name) * 105, 10, 0, None)
            elif key == ord("o"):
                dashboard.add_source(False)
            elif key == ord("u"):
                dashboard.add_source(True)
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
