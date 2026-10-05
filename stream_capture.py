import cv2
from threading import Lock
from PySide6.QtCore import QThread, Signal
from time import monotonic  # 다음 영상을 읽을 시간 계산

class StreamCapture(QThread):
    ready = Signal(int)       # 첫 영상 수신 성공과 작업 번호 전달
    error = Signal(str, int)  # 연결 오류와 작업 번호 전달

    def __init__(self, url, task_id, parent=None):
        super().__init__(parent)
        self.url = url  # 실제 영상 주소
        self.task_id = task_id  # 현재 연결 작업 번호
        self.latest_frame = None  # 가장 최근에 받은 영상 한 장
        self.lock = Lock()  # 영상 저장과 가져오기가 겹치지 않게 보호

    def take_frame(self):
        with self.lock:
            frame = self.latest_frame  # 최근 영상 가져오기
            self.latest_frame = None  # 같은 영상을 다시 처리하지 않기
        return frame

    def stop(self):
        self.requestInterruption()  # 영상 읽기 종료 요청

    def run(self):
        capture = cv2.VideoCapture()  # 이 작업 안에서만 영상 연결 사용
        try:
            opened = capture.open(
                self.url,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 20000
                ]
            )  # 연결은 최대 10초, 한 장 읽기는 최대 5초 기다림

            if self.isInterruptionRequested():
                return
            if not opened:
                raise RuntimeError("실시간 영상을 열 수 없습니다.")

            fps = capture.get(cv2.CAP_PROP_FPS)  # 원본 영상의 초당 프레임 수
            fps = fps if 1 <= fps <= 60 else 30  # FPS 정보가 이상하면 30 사용
            frame_interval = 1.0 / fps  # 영상 한 장 사이의 시간
            next_frame_time = monotonic()  # 다음 영상을 읽을 기준 시간

            first_frame = True  # 첫 영상 수신 여부
            while not self.isInterruptionRequested():
                ok, frame = capture.read()  # 영상 한 장 읽기
                if self.isInterruptionRequested():
                    break
                if not ok:
                    raise RuntimeError("영상 수신이 끊겼습니다. 다시 연결해 주세요.")

                with self.lock:
                    self.latest_frame = frame  # 이전 영상 대신 최신 영상 보관

                if first_frame:
                    self.ready.emit(self.task_id)  # 첫 영상 수신 알림
                    first_frame = False

                next_frame_time += frame_interval  # 매 프레임마다 시간 계산
                delay = next_frame_time - monotonic()  # 기다릴 시간 계산
                if delay > 0:
                    self.msleep(max(1, round(delay * 1000)))  # 원본 FPS에 맞춰 읽기
                else:
                    next_frame_time = monotonic()  # 수신이 늦으면 기준 갱신
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.error.emit(str(exc), self.task_id)
        finally:
            capture.release()  # 종료하거나 오류가 나면 연결 해제