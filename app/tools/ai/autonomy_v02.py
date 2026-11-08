"""Opt-in image-space safety assistant. It never creates flight or gimbal commands."""

from collections import deque
from dataclasses import dataclass
import math
import threading

from tools.ai.crowd_regions import CrowdRegions


@dataclass(frozen=True)
class Candidate:
    kind: str
    xyxy: tuple
    confidence: float
    source: str = "YOLO"
    track_id: int | None = None

    @property
    def center(self):
        x1, y1, x2, y2 = self.xyxy
        return ((x1 + x2) / 2, (y1 + y2) / 2)


class RateMeter:
    def __init__(self, window=2.0):
        self.window = window
        self.times = deque()

    def mark(self, now):
        self.times.append(now)
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()

    def fps(self, now):
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()
        return ((len(self.times) - 1) / (self.times[-1] - self.times[0])
                if len(self.times) >= 2 and self.times[-1] > self.times[0] else 0.0)


def crop_around(box, width, height, expansion=2.0, minimum=192):
    x1, y1, x2, y2 = box
    side = min(width, height, max(minimum, int(max(x2 - x1, y2 - y1) * expansion)))
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    left = max(0, min(width - side, int(cx - side / 2)))
    top = max(0, min(height - side, int(cy - side / 2)))
    return (left, top, min(width, left + side), min(height, top + side))


class AutoRoiPreview:
    """Keep a validated crop from its analysis frame, independent of UI paint FPS."""

    def __init__(self, live_seconds=0.7, wait_seconds=2.0):
        self.live_seconds, self.wait_seconds = live_seconds, wait_seconds
        self.lock = threading.Lock()
        self.crop = None
        self.kind = None
        self.frame_at = 0.0
        self.sequence = -1
        self.active = False
        self.status = "WAITING"
        self.status_revision = 0
        self.status_changes = {"LIVE": 0, "HOLD": 0, "WAITING FOR TARGET": 0,
                               "WAITING FOR VIDEO": 0}

    def reset(self):
        with self.lock:
            self.crop, self.kind, self.frame_at = None, None, 0.0
            self.sequence, self.active = -1, False
            self.status = "WAITING"
            self.status_revision += 1
            self.status_changes = {name: 0 for name in self.status_changes}

    def capture(self, frame, result, received_at, sequence):
        """Reject invalid/blank images before replacing the last good crop."""
        import numpy as np
        roi = result.get("digital_roi") if result else None
        if (roi is None or frame is None or not isinstance(frame, np.ndarray)
                or frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8
                or received_at <= 0 or sequence < 0):
            return False
        try:
            if len(roi) != 4 or not all(math.isfinite(float(value)) for value in roi):
                return False
            x1, y1, x2, y2 = (int(value) for value in roi)
        except (TypeError, ValueError, OverflowError):
            return False
        height, width = frame.shape[:2]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height
                and x2 - x1 >= 16 and y2 - y1 >= 16):
            return False
        crop = frame[y1:y2, x1:x2]
        sample = crop[::4, ::4]
        if np.count_nonzero(sample > 12) < max(8, sample.size // 1000):
            return False
        copy = crop.copy()
        with self.lock:
            if sequence <= self.sequence or received_at <= self.frame_at:
                return False
            self.crop, self.kind = copy, result.get("target_kind") or "TARGET"
            self.frame_at, self.sequence, self.active = received_at, sequence, True
        return True

    def snapshot(self, now, latest_received_at):
        with self.lock:
            if not self.active:
                return None
            age = max(0.0, now - self.frame_at)
            video_age = max(0.0, now - latest_received_at)
            if age >= self.wait_seconds:
                status = "WAITING FOR VIDEO" if video_age >= self.live_seconds else "WAITING FOR TARGET"
                crop = None
            elif age >= self.live_seconds or video_age >= self.live_seconds:
                status, crop = "HOLD", self.crop
            else:
                status, crop = "LIVE", self.crop
            if status != self.status:
                self.status = status
                self.status_revision += 1
                self.status_changes[status] += 1
            return {"crop": crop, "kind": self.kind, "status": self.status,
                    "status_revision": self.status_revision, "sequence": self.sequence,
                    "age_ms": round(age * 1000), "status_changes": self.status_changes.copy()}


def project_world_point(target_xyz, camera_xyz, yaw_rad=0.0, width=640, height=480, hfov=1.74):
    """Static ENU pinhole estimate, not a calibrated moving-drone projection."""
    dx, dy, dz = (target_xyz[i] - camera_xyz[i] for i in range(3))
    forward = math.cos(yaw_rad) * dx + math.sin(yaw_rad) * dy
    if forward <= 0.1:
        return None
    right = math.sin(yaw_rad) * dx - math.cos(yaw_rad) * dy
    focal = width / (2 * math.tan(hfov / 2))
    return (width / 2 + focal * right / forward,
            height / 2 - focal * dz / forward)


def place_track_label(text, box, frame_size, occupied, scale=0.45):
    """Choose an unoccupied label origin; None means no honest readable placement."""
    import cv2
    width, height = frame_size
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x1, y1, x2, y2 = box
    candidates = [(x1, y1 - 5), (x1 + 2, y1 + th + 3),
                  (x2 - tw, y1 + th + 3), (x1, y2 + th + 5),
                  (x2 - tw, y1 - 5), (x2 + 3, y1 + th),
                  (x1 - tw - 3, y1 + th)]
    for x, y in candidates:
        x, y = int(x), int(y)
        rect = (x - 1, y - th - 1, x + tw + 1, y + base + 1)
        if rect[0] < 0 or rect[1] < 0 or rect[2] >= width or rect[3] >= height:
            continue
        if any(rect[0] < o[2] and rect[2] > o[0] and rect[1] < o[3] and rect[3] > o[1]
               for o in occupied):
            continue
        occupied.append(rect)
        return (x, y)
    return None


def compact_track_label(track_id, box, frame_size, occupied):
    """Reserve space for a short ID, then a number; never invent a track."""
    for label in (f"ID:{track_id}", str(track_id)):
        origin = place_track_label(label, box, frame_size, occupied)
        if origin is not None:
            return label, origin
    return None, None


class CrowdRoiSmoother:
    """Analysis-frame crop with a stable 2x view and a 3x compact-group limit."""

    def __init__(self):
        self.view = None

    def reset(self):
        self.view = None

    def crop(self, box, width, height):
        x1, y1, x2, y2 = box
        aspect = width / height
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        # Preserve every visible member, including when the group spans most of the frame.
        fit_width = max(bw * 1.15, bh * aspect * 1.15)
        zoom_width = width / (3 if bw < width * .17 and bh < height * .2 else 2)
        target_width = min(width, max(fit_width, zoom_width))
        target = ((x1 + x2) / 2, (y1 + y2) / 2, target_width)
        if self.view is None:
            self.view = target
        else:
            old = self.view
            # Subpixel detector jitter does not move the viewport.
            values = tuple(old[i] if abs(target[i] - old[i]) < (4 if i < 2 else 8)
                           else old[i] + .25 * (target[i] - old[i]) for i in range(3))
            self.view = values
        cx, cy, crop_width = self.view
        # A viewport must still contain the current group during smoothing.
        crop_width = min(width, max(crop_width, bw * 1.1, bh * aspect * 1.1))
        crop_height = crop_width / aspect
        cx = max(x2 - crop_width / 2, min(x1 + crop_width / 2, cx))
        cy = max(y2 - crop_height / 2, min(y1 + crop_height / 2, cy))
        left = max(0, min(width - crop_width, cx - crop_width / 2))
        top = max(0, min(height - crop_height, cy - crop_height / 2))
        return (int(left), int(top), min(width, int(left + crop_width)),
                min(height, int(top + crop_height)))


def choose_candidate(candidates):
    priority = {"FIRE": 4, "SMOKE": 3, "CROWD": 2, "HUMAN": 1}
    eligible = [c for c in candidates if c.kind in priority]
    return max(eligible, key=lambda c: (priority[c.kind], c.confidence), default=None)


class TargetLock:
    """Hold a visible target; require sustained higher-risk evidence to switch."""

    PRIORITY = {"FIRE": 4, "SMOKE": 3, "CROWD": 2, "HUMAN": 1}

    def __init__(self, missing_grace=.8, promotion_seconds=.5):
        self.missing_grace = missing_grace
        self.promotion_seconds = promotion_seconds
        self.target = None
        self.last_seen = None
        self.challenger = None
        self.challenger_since = None
        self.switches = 0

    @staticmethod
    def matches(left, right):
        if left.kind != right.kind:
            return False
        if left.track_id is not None or right.track_id is not None:
            return left.track_id is not None and left.track_id == right.track_id
        ax, ay = left.center
        bx, by = right.center
        aw = max(1, left.xyxy[2] - left.xyxy[0])
        bw = max(1, right.xyxy[2] - right.xyxy[0])
        ah = max(1, left.xyxy[3] - left.xyxy[1])
        bh = max(1, right.xyxy[3] - right.xyxy[1])
        return math.hypot(ax - bx, ay - by) <= max(32, min(aw, bw, ah, bh) * .6)

    def select(self, candidates, now):
        pool = [c for c in candidates if c.kind in self.PRIORITY]
        if self.target is None:
            chosen = choose_candidate(pool)
            if chosen:
                self.target, self.last_seen = chosen, now
            return chosen, bool(chosen)
        matching = [c for c in pool if self.matches(self.target, c)]
        current = max(matching, key=lambda c: c.confidence, default=None)
        if current:
            self.target, self.last_seen = current, now
        else:
            if self.last_seen is None or now - self.last_seen > self.missing_grace:
                self.target = None
                self.challenger = None
                self.challenger_since = None
                return self.select(pool, now)
        higher = [c for c in pool if self.PRIORITY[c.kind] > self.PRIORITY[self.target.kind]
                  and c.source != "WORLD_PROBE_SIM_ONLY"]
        proposed = choose_candidate(higher)
        if proposed is not None:
            if self.challenger is None or not self.matches(self.challenger, proposed):
                self.challenger, self.challenger_since = proposed, now
            elif now - self.challenger_since >= self.promotion_seconds:
                self.target, self.last_seen = proposed, now
                self.challenger, self.challenger_since = None, None
                self.switches += 1
                return proposed, True
        else:
            self.challenger, self.challenger_since = None, None
        # Missing targets stay locked for reacquisition but never become fake LIVE crops.
        return current, False


class AutoSafetyController:
    def __init__(self):
        self.enabled = False
        self.state = "PATROL"
        self.since = 0.0
        self.last_seen = None
        self.candidate_key = None
        self.hits = 0
        self.last_retry = -1e9
        self.crowd = CrowdRegions()
        self.crowd_roi = CrowdRoiSmoother()
        self.target_lock = TargetLock()
        self.latest = self._result(None, None, "OFF", None, False)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.state = "PATROL"
        self.since = 0.0
        self.last_seen = None
        self.candidate_key = None
        self.hits = 0
        self.last_retry = -1e9
        self.crowd = CrowdRegions()
        self.crowd_roi.reset()
        self.target_lock = TargetLock()
        self.latest = self._result(None, None, "ON" if enabled else "OFF", None, False)

    def _result(self, candidate, aim, reason, crowd, retry):
        return {"enabled": self.enabled, "state": self.state, "reason": reason,
                "target": candidate.center if candidate else None,
                "target_box": candidate.xyxy if candidate else None,
                "target_kind": candidate.kind if candidate else None,
                "target_confidence": candidate.confidence if candidate else None,
                "target_source": candidate.source if candidate else None,
                "virtual_aim": aim, "digital_roi": None,
                "roi_retry_allowed": retry, "crowd": crowd,
                "target_switches": self.target_lock.switches,
                "flight_command": None, "gimbal_command": None}

    def _transition(self, state, now):
        if self.state != state:
            self.state, self.since = state, now

    def update(self, *, now, width, height, candidates, tracks=(), source_age_ms=0,
               analysis_ms=0, confirmed_kinds=(), px4_connected=False, world_probe=None):
        if not self.enabled:
            return self.latest
        crowd = self.crowd.update(tracks, now, width, height)
        pool = list(candidates)
        active_groups = [group for group in crowd["groups"]
                         if group["level"] in ("CAUTION", "HIGH") and not group["stale"]]
        # Legacy CROWD warning still uses its original count; only the preview
        # follows a measured compact group when one exists.
        if active_groups:
            pool = [item for item in pool
                    if not (item.kind == "CROWD" and item.source == "LEGACY_CONFIRMED")]
        for group in active_groups:
            pool.append(Candidate("CROWD", tuple(group["xyxy"]), 1.0,
                                  "IMAGE-SPACE / UNCALIBRATED", track_id=group["group_id"]))
        if world_probe is not None and not pool:
            x, y = world_probe
            if 0 <= x < width and 0 <= y < height:
                pool.append(Candidate("FIRE", (x - 24, y - 24, x + 24, y + 24),
                                      0.0, "WORLD_PROBE_SIM_ONLY"))
        fresh = source_age_ms <= 500
        if not fresh:
            self._transition("RESUME", now)
            candidate = None
            reason = "STALE FRAME"
        else:
            candidate, target_changed = self.target_lock.select(pool, now)
            if target_changed:
                self.candidate_key, self.hits = None, 0
                self.crowd_roi.reset()
                self._transition("SUSPECTED", now)
            reason = "VIRTUAL ONLY" if px4_connected else "PX4 UNAVAILABLE / VIRTUAL ONLY"
        if candidate:
            key = (candidate.kind, candidate.track_id if candidate.track_id is not None else "LOCKED")
            if key == self.candidate_key and self.last_seen is not None and now - self.last_seen <= 0.8:
                self.hits += 1
            else:
                self.hits = 1
            self.candidate_key, self.last_seen = key, now
            if self.state in ("PATROL", "RESUME"):
                self._transition("SUSPECTED", now)
            elif self.state == "SUSPECTED" and self.hits >= 2:
                self._transition("AIMING", now)
            elif self.state == "AIMING" and now - self.since >= 0.3:
                self._transition("VERIFYING", now)
            elif (self.state == "VERIFYING" and candidate.kind in confirmed_kinds
                  and candidate.source != "WORLD_PROBE_SIM_ONLY"):
                self._transition("ALERT", now)
        elif self.last_seen is not None and now - self.last_seen > 0.7:
            self._transition("RESUME", now)
            self.hits = 0
        if (self.state == "VERIFYING" and now - self.since > 3.0
                and (not candidate or candidate.kind not in confirmed_kinds)):
            self._transition("RESUME", now)
        if self.state == "ALERT" and (not candidate or candidate.kind not in confirmed_kinds):
            self._transition("RESUME", now)
        if self.state == "RESUME" and now - self.since >= 1.0:
            self._transition("PATROL", now)
        aim = None
        retry = False
        if candidate and self.state in ("AIMING", "VERIFYING", "ALERT"):
            x, y = candidate.center
            aim = {"dx_normalized": round((x - width / 2) / (width / 2), 3),
                   "dy_normalized": round((y - height / 2) / (height / 2), 3),
                   "target_px": (round(x), round(y)), "actuation": "DISABLED"}
            retry = (self.state == "VERIFYING" and candidate.kind in ("FIRE", "SMOKE")
                     and analysis_ms < 80 and now - self.last_retry >= 1.5 and fresh)
        result = self._result(candidate, aim, reason, crowd, retry)
        if candidate and aim:
            result["digital_roi"] = (self.crowd_roi.crop(candidate.xyxy, width, height)
                                     if candidate.kind == "CROWD" else
                                     crop_around(candidate.xyxy, width, height))
            roi = result["digital_roi"]
            result["digital_roi_zoom"] = round(width / max(1, roi[2] - roi[0]), 2)
        self.latest = result
        return result

    def note_retry(self, now):
        self.last_retry = now


def fire_roi_retry(frame, roi, model, device):
    """Run the existing fire model on original-color pixels; findings are candidates only."""
    import cv2
    x1, y1, x2, y2 = roi
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return []
    scaled = cv2.resize(crop, (640, 640), interpolation=cv2.INTER_LINEAR)
    result = model.predict(scaled, device=device, conf=0.30, iou=0.70, imgsz=640,
                           rect=True, agnostic_nms=False, max_det=300, verbose=False)[0]
    output = []
    for box in result.boxes:
        name = str(result.names[int(box.cls[0])]).upper()
        kind = "FIRE" if "FIRE" in name or "FLAME" in name else "SMOKE" if "SMOKE" in name else None
        if kind is None:
            continue
        a, b, c, d = box.xyxy[0].tolist()
        mapped = (x1 + a * (x2 - x1) / 640, y1 + b * (y2 - y1) / 640,
                  x1 + c * (x2 - x1) / 640, y1 + d * (y2 - y1) / 640)
        output.append(Candidate(kind, mapped, float(box.conf[0]), "ROI_RETRY"))
    return output
