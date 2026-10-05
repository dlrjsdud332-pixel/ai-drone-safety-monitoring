import cv2
from pathlib import Path
from ultralytics import YOLO
from time import monotonic  # 시간이 얼마나 지났는지 측정\


class AITracker:
    def __init__(self):
        model_path = Path(__file__).resolve().parent / "models" / "yolo11s.pt"  # 모델 위치
        self.model = YOLO(str(model_path))  # 기존 객체 탐지 모델 불러오기
        self.box_history = {}  # ID별 마지막 박스와 감지 시간 저장

    def process(self, frame):
        result = self.model.track(
            frame, persist=True, tracker="bytetrack.yaml",  # 추적 ID 유지
            classes=[0, 2, 3, 5, 7],  # 사람·자동차·오토바이·버스·트럭
            conf=0.10, imgsz=960, device="mps", verbose=False  # 약한 감지 결과도 기존 대상 추적에 활용
        )[0]
        keep = []  # 화면에 표시할 결과 번호
        for i, box in enumerate(result.boxes):
            class_id = int(box.cls[0])  # 0은 사람, 나머지는 차량
            confidence = float(box.conf[0])  # 감지 신뢰도
            if class_id == 0 or confidence >= 0.40:  # 사람은 유지, 차량은 50% 이상만 사용
                keep.append(i)
        result = result[keep]  # 박스·개수·선택 대상에 같은 필터 적용
        people = 0
        vehicles = 0
        objects = []  # 클릭할 대상의 ID와 박스 좌표 보관
        for box in result.boxes:
            class_id = int(box.cls[0])  # 사람은 0, 나머지는 차량
            if class_id == 0:
                people += 1
            else:
                vehicles += 1
            if box.id is not None:  # 추적 ID가 있는 대상만 선택 가능
                track_id = int(box.id[0])
                bbox = tuple(map(int, box.xyxy[0].tolist()))  # 왼쪽·위·오른쪽·아래 좌표
                name = {0: "사람", 2: "자동차", 3: "오토바이", 5: "버스", 7: "트럭"}.get(class_id, "객체")  # 종류별 표시 이름
                objects.append({"id": track_id, "bbox": bbox, "name": name})  # 이름도 UI에 전달

        annotated = result.plot(labels=False, conf=False, line_width=1)  # 이름표 숨김, 박스 선을 얇게 표시
        return annotated, people, vehicles, objects  # 현재 대상 정보 전달