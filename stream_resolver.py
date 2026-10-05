import sys
import subprocess
from PySide6.QtCore import QThread, Signal


class StreamResolver(QThread):
    resolved = Signal(str, int)  # 추출한 영상 주소와 작업 번호 전달
    error = Signal(str, int)  # 오류 내용과 작업 번호 전달

    def __init__(self, url, task_id, parent=None):
        super().__init__(parent)
        self.url = url  # 입력한 유튜브 주소
        self.task_id = task_id  # 현재 연결 작업 번호

    def run(self):
        try:
            command = [
                sys.executable, "-m", "yt_dlp",
                "-f", "best[height<=1080]/bestvideo[height<=1080]",  # 제공되는 영상 중 1080p 이하 선택
                "--get-url", "--no-playlist", self.url
            ]  # 1080p 이하의 실제 영상 주소 추출

            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )  # 별도 작업에서 실행하며 최대 30초 기다림

            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "영상 주소 추출 실패")

            urls = result.stdout.strip().splitlines()  # 출력된 주소를 줄별로 분리
            if not urls:
                raise RuntimeError("영상 주소가 없습니다.")

            self.resolved.emit(urls[0], self.task_id)  # 첫 번째 주소 전달

        except subprocess.TimeoutExpired:
            self.error.emit("주소 추출 시간이 초과됐습니다.", self.task_id)
        except Exception as exc:
            self.error.emit(str(exc), self.task_id)