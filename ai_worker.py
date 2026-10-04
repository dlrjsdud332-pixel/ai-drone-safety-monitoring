from threading import Condition
from PySide6.QtCore import QThread, Signal
from ai_tracker import AITracker
from time import perf_counter  # AI 처리 시간을 정확하게 측정

class AIWorker(QThread):
    result_ready = Signal(object, int, int, object, int, object, float)  # 마지막에 AI 처리 FPS 전달
    error = Signal(str)  # 오류 내용을 화면에 전달

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
                ai_fps = 1.0 / max(perf_counter() - started, 0.000001)  # 초당 분석 가능한 프레임 수
                self.result_ready.emit(annotated, people, vehicles, objects, task_id, frame.copy(), ai_fps)

        except Exception as exc:
            self.error.emit(str(exc))  # 분석 오류 전달