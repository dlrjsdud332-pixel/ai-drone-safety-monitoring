from pathlib import Path
from ultralytics import YOLO

class AITracker:
    def __init__(self):
        model_path = Path(__file__).resolve().parent / "models" / "yolo11s.pt"  # 모델 위치
        self.model = YOLO(str(model_path))  # 기존 객체 탐지 모델 불러오기

    def process(self, frame):
        result = self.model.track(
            frame, persist=True, tracker="bytetrack.yaml",  # 추적 ID 유지
            classes=[0, 2, 3, 5, 7],  # 사람·자동차·오토바이·버스·트럭
            conf=0.25, imgsz=640, device="mps", verbose=False  # 맥 GPU 사용
        )[0]
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

        annotated = result.plot(conf=False, line_width=2)  # 탐지 박스 표시
        return annotated, people, vehicles, objects  # 대상 정보도 함께 전달