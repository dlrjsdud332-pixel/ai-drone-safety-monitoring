from pathlib import Path
from ultralytics import YOLO

class PPEDetector:
    def __init__(self):
        path = Path(__file__).resolve().parent / "models" / "ppe_best.pt"  # 기존 보호구 모델
        self.model = YOLO(str(path))
        self.items = {1: "NO_HARDHAT", 2: "NO_SAFETY_VEST"}  # 모델의 미착용 클래스

    def detect(self, frame, objects):
        result = self.model.predict(
            frame, conf=0.50, imgsz=640, device="mps", verbose=False
        )[0]  # 신뢰도 50% 이상인 보호구 탐지 결과
        violations = []  # 사람별 미착용 결과를 담을 목록

        for obj in objects:
            if obj["name"] != "사람" or obj["id"] < 0:
                continue  # 차량과 추적 번호 없는 대상 제외
            x1, y1, x2, y2 = obj["bbox"]
            missing = set()  # 같은 항목의 중복 탐지 제거

            for box in result.boxes:
                item = int(box.cls[0])  # 탐지된 보호구 클래스 번호
                if item not in self.items:
                    continue
                px1, py1, px2, py2 = box.xyxy[0].tolist()
                cx, cy = (px1 + px2) / 2, (py1 + py2) / 2  # 보호구 탐지 박스 중심
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    missing.add(self.items[item])  # 사람 박스 안의 미착용 항목

            for item in sorted(missing):
                violations.append({"track_id": obj["id"], "missing_item": item})

        return violations  # 추적 ID와 미착용 항목 전달