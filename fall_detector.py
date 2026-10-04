from pathlib import Path
from ultralytics import YOLO


class FallDetector:
    def __init__(self):
        path = Path(__file__).resolve().parent / "models" / "yolo11n-pose.pt"
        self.model = YOLO(str(path))  # 기존 자세 탐지 모델 불러오기
        self.was_upright = {}  # 사람 ID별로 이전에 서 있었는지 저장

    def reset(self):
        self.was_upright.clear()  # 영상이 바뀌면 이전 자세 기록 초기화

    def detect(self, frame, objects):
        result = self.model.predict(
            frame, conf=0.35, imgsz=640, device="mps", verbose=False
        )[0]  # 현재 영상에서 사람의 자세 탐지
        falls = set()  # 이번 검사에서 넘어짐이 의심되는 사람 ID

        for pose_box in result.boxes.xyxy:
            x1, y1, x2, y2 = map(float, pose_box.tolist())
            best_id = None  # 자세 박스와 가장 잘 겹치는 추적 ID
            best_overlap = 0.0  # 두 박스가 겹치는 비율

            for obj in objects:
                if obj["name"] != "사람" or obj["id"] < 0:
                    continue  # 추적 ID가 있는 사람만 비교

                bx1, by1, bx2, by2 = obj["bbox"]
                width = max(0, min(x2, bx2) - max(x1, bx1))
                height = max(0, min(y2, by2) - max(y1, by1))
                intersection = width * height  # 두 박스가 겹치는 면적
                area_pose = (x2 - x1) * (y2 - y1)
                area_track = (bx2 - bx1) * (by2 - by1)
                overlap = intersection / (area_pose + area_track - intersection + 1e-6)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_id = obj["id"]  # 가장 많이 겹치는 사람 선택

            if best_id is None or best_overlap < 0.3:
                continue  # 겹침이 부족하면 사람을 연결하지 않음

            width = x2 - x1
            height = y2 - y1

            if width > height * 1.8:
                if self.was_upright.get(best_id, False):
                    falls.add(best_id)  # 서 있던 사람이 누운 형태로 바뀜
                    self.was_upright[best_id] = False  # 같은 자세의 반복 감지 방지
            elif height > width * 1.2:
                self.was_upright[best_id] = True  # 서 있는 형태를 기억

        return sorted(falls)  # 넘어짐 의심 ID 목록 반환