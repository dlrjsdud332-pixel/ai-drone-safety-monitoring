"""Shared, display-only label placement for detections on one camera frame."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelRequest:
    key: str
    texts: tuple[str, ...]
    box: tuple[int, int, int, int]
    color: tuple[int, int, int]
    priority: int


class DetectionLabelLayout:
    def __init__(self, frame_size, previous=None):
        self.width, self.height = frame_size
        self.previous = previous or {}
        self.requests = []
        self.anchors = {}
        self.placed = []
        self.omitted = []

    def add(self, key, texts, box, color, priority):
        self.requests.append(LabelRequest(str(key), tuple(texts), tuple(map(int, box)),
                                          tuple(color), priority))

    @staticmethod
    def intersects(a, b):
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    def draw(self, image, reserved=()):
        import cv2
        occupied = list(reserved)
        # Hazards reserve space before PPE and the many crowd IDs.
        for request in sorted(self.requests, key=lambda r: (-r.priority, r.key)):
            x1, y1, x2, y2 = request.box
            chosen = None
            human = request.key.startswith("HUMAN:")
            hazard = request.key.startswith(("FIRE:", "SMOKE:"))
            font_scale = .34 if human else (.35 if hazard else .40)
            pad_x, pad_y = (2, 1) if human or hazard else (4, 3)
            for text in request.texts:
                (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
                label_w, label_h = tw + 2 * pad_x, th + baseline + 2 * pad_y
                positions = [(x1, y1 - label_h - 2), (x1 + 2, y1 + 2),
                             (x2 - label_w, y1 + 2), (x2 + 3, y1),
                             (x1 - label_w - 3, y1), (x1, y2 + 2)]
                preferred = self.previous.get(request.key)
                if preferred is not None and preferred < len(positions):
                    positions = [(preferred, positions[preferred])] + [
                        (i, pos) for i, pos in enumerate(positions) if i != preferred]
                else:
                    positions = list(enumerate(positions))
                for anchor, (x, y) in positions:
                    rect = (int(x), int(y), int(x + label_w), int(y + label_h))
                    if (rect[0] < 0 or rect[1] < 0 or rect[2] >= self.width
                            or rect[3] >= self.height or any(self.intersects(rect, used) for used in occupied)
                            or any(self.intersects(rect, other.box) for other in self.requests
                                   if other is not request)):
                        continue
                    chosen = (text, rect, anchor, th, baseline, font_scale, pad_x, pad_y, human)
                    break
                if chosen:
                    break
            if chosen is None:
                self.omitted.append(request.key)
                continue
            text, rect, anchor, th, baseline, font_scale, pad_x, pad_y, human = chosen
            occupied.append(rect)
            self.anchors[request.key] = anchor
            self.placed.append((request.key, text, rect))
            if human:
                x0, y0, xend, yend = rect
                region = image[y0:yend, x0:xend]
                tint = region.copy()
                tint[:] = request.color
                cv2.addWeighted(tint, .38, region, .62, 0, dst=region)
            else:
                cv2.rectangle(image, rect[:2], rect[2:], request.color, -1)
            cv2.putText(image, text, (rect[0] + pad_x, rect[1] + pad_y + th),
                        cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), 1, cv2.LINE_AA)
        return image
