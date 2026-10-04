from threading import Condition
from PySide6.QtCore import QThread, Signal
from ai_tracker import AITracker
from time import perf_counter         # AI 처리 시간을 정확하게 측정
from ppe_detector import PPEDetector  # 보호구 미착용 감지 기능
from fall_detector import FallDetector  # 넘어짐 의심 감지 기능



class AIWorker(QThread):
    result_ready = Signal(object, int, int, object, int, object, float)  # 마지막에 AI 처리 FPS 전달
    error = Signal(str)  # 오류 내용을 화면에 전달
    ppe_ready = Signal(object, int)  # 미착용 결과와 영상 작업 번호 전달
    fall_ready = Signal(object, int)  # 넘어짐 의심 ID 목록과 영상 작업 번호 전달
    def __init__(self, parent=None):
        super().__init__(parent)
        self.condition = Condition()  # 작업 전달과 대기 관리
        self.pending = None  # 다음에 분석할 영상
        self.stopping = False  # 종료 요청 여부

    def submit(self, frame, task_id):
        with self.condition:
            self.pending = (frame.copy(), task_id)  # 최신 영상만 보관
            self.condition.notify()  # 대기 중인 AI 깨우기

    def stop(self):
        with self.condition:
            self.stopping = True  # 작업 종료 요청
            self.pending = None  # 남은 영상 비우기
            self.condition.notify()  # 대기를 풀어 종료하도록 처리

    def run(self):
        try:
            tracker = AITracker()  # 별도 스레드에서 모델 불러오기
            ppe = PPEDetector()  # AI 스레드에서 보호구 모델 불러오기
            fall = FallDetector()  # AI 스레드에서 자세 모델 불러오기
            frame_count = 0  # 분석한 프레임 수
            previous_task = None  # 이전 영상 작업 번호
            while True:
                with self.condition:
                    while self.pending is None and not self.stopping:
                        self.condition.wait()  # 영상이 들어올 때까지 대기
                    if self.stopping:
                        return  # 종료 요청이면 작업 끝내기
                    frame, task_id = self.pending  # 분석할 영상 가져오기
                    self.pending = None  # 대기 공간 비우기

                started = perf_counter()  # 분석 시작 시각
                annotated, people, vehicles, objects = tracker.process(frame)  # 탐지와 추적
                if task_id != previous_task:
                    frame_count = 0  # 영상 변경 시 검사 간격 초기화
                    fall.reset()  # 이전 영상에서 기억한 자세 초기화
                    previous_task = task_id
                frame_count += 1  # 분석 프레임 수 증가
                if frame_count % 10 == 0:
                    violations = ppe.detect(frame, objects)  # 보호구 미착용 검사
                    self.ppe_ready.emit(violations, task_id)
                    falls = fall.detect(frame, objects)  # 넘어짐 의심 검사
                    self.fall_ready.emit(falls, task_id)
                ai_fps = 1.0 / max(perf_counter() - started, 0.000001)  # 초당 분석 가능한 프레임 수
                self.result_ready.emit(annotated, people, vehicles, objects, task_id, frame.copy(), ai_fps)

        except Exception as exc:
            self.error.emit(str(exc))  # 분석 오류 전달