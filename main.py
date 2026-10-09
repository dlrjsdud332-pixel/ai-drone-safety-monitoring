import sys
import cv2                                      # 영상 프레임 읽기
from threading import Condition, RLock
from functools import wraps
import torch
from ppe_detector import PPEDetector
from fall_detector import FallDetector
from time import perf_counter
from ai_tracker import AITracker
from PySide6.QtCore import QThread
import csv                                      # 표 형태의 이벤트 기록 저장
from ai_worker import AIWorker                  # 별도 스레드에서 AI 분석
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QSlider           # 재생 위치 조절 막대
from PySide6.QtCore import QPoint, QRect, Signal  # 좌표와 사각형, 선택 완료 신호
from PySide6.QtWidgets import QRubberBand  # 드래그 선택 사각형
from PySide6.QtCore import QTimer          # 일정 간격으로 화면 갱신
from PySide6.QtWidgets import QLineEdit
from PySide6.QtGui import QImage, QPixmap  # OpenCV 영상을 UI 이미지로 변환
from PySide6.QtGui import QShortcut, QKeySequence  # Esc 단축키 설정
from PySide6.QtWidgets import QSizePolicy  # 영상 영역의 크기 조절
from PySide6.QtGui import QPolygonF        # 여러 점으로 만든 다각형
from pathlib import Path                   # 파일 경로와 이름 처리
from PySide6.QtWidgets import QFileDialog, QListWidgetItem  # 파일 선택 창과 목록 항목
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QWidget, QHBoxLayout
from PySide6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QListWidget
from PySide6.QtGui import QPainter, QPen, QColor  # 화면에 점·선·색을 그리는 도구
from PySide6.QtCore import QPointF         # 소수점 좌표를 저장하는 도구
from PySide6.QtWidgets import QComboBox    # 여러 항목 중 하나를 선택하는 메뉴
from datetime import datetime              # 경고가 발생한 시각
from time import monotonic                 # 중복 경고 사이의 시간 계산
from PySide6.QtCore import Qt, QObject, QEvent
from stream_resolver import StreamResolver # 유튜브 영상 주소 추출
from stream_capture import StreamCapture  # 별도 작업에서 실시간 영상 읽기


gpu_analysis_lock = RLock()  # 모든 AI 모델이 같은 맥 GPU를 순서대로 사용

def serialize_gpu_call(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        with gpu_analysis_lock:
            try:
                return method(*args, **kwargs)
            finally:
                if torch.backends.mps.is_available():
                    torch.mps.synchronize()  # GPU 작업 완료 후 다음 분석에 차례 넘기기
    return guarded

# 첫 화면의 감지·보호구·넘어짐과 두 번째 화면의 감지를 같은 잠금으로 보호
for detector, methods in ((AITracker, ("__init__", "process")),
                          (PPEDetector, ("__init__", "detect")),
                          (FallDetector, ("__init__", "detect"))):
    for name in methods:
        setattr(detector, name, serialize_gpu_call(getattr(detector, name)))

class StatsCards(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(62)  # 정보 카드를 작게 고정해 영상 공간 확보
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.values = []
        for title, unit in (("사람", "명"), ("차량", "대"), ("FPS", "fps")):
            card = QFrame()
            card.setObjectName("statCard")
            card.setStyleSheet("QFrame#statCard {background:#111F2E; border:1px solid #263B50; border-radius:8px;}")
            column = QVBoxLayout(card)
            column.setContentsMargins(8, 5, 8, 5)
            column.setSpacing(2)
            name = QLabel(title)
            name.setStyleSheet("color:#91A7BD; font-size:11px; font-weight:normal;")
            value = QLabel("00" if unit != "fps" else "0.0")
            value.setStyleSheet("color:#E8F0FA; font-size:18px; font-weight:bold;")
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            value.setToolTip("AI 처리 속도" if unit == "fps" else title + " 감지 수")
            column.addWidget(name)
            column.addWidget(value)
            row.addWidget(card, 1)
            self.values.append((value, unit))
        self.set_values(0, 0, 0.0)

    def set_values(self, people, vehicles, fps):
        for (label, unit), value in zip(self.values, (people, vehicles, fps)):
            number = f"{value:.1f}" if unit == "fps" else f"{int(value):02d}"
            label.setText(number + f' <span style="font-size:11px; color:#91A7BD;">{unit}</span>')
            label.setToolTip(f"AI 처리 {value:.1f} FPS" if unit == "fps" else f"{int(value)}{unit}")


class SecondAIWorker(QThread):
    result_ready = Signal(int, int, float, int, object, object)  # 사람 수, 차량 수, 분석 FPS, 작업 번호
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.condition = Condition()
        self.pending = None
        self.stopping = False

    def submit(self, frame, task_id):
        with self.condition:
            if not self.stopping:
                self.pending = (frame.copy(), task_id)  # 오래된 프레임 대신 최신 한 장만 보관
                self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.pending = None
            self.condition.notify()

    def run(self):
        tracker = None
        previous_task = None
        while True:
            with self.condition:
                while self.pending is None and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                frame, task_id = self.pending
                self.pending = None
            try:
                if previous_task != task_id:
                    tracker = AITracker()  # 영상별 추적 정보 분리, 첫 번째 화면의 모델과 독립
                    previous_task = task_id
                started = perf_counter()
                _, people, vehicles, objects = tracker.process(frame)
                fps = 1.0 / max(perf_counter() - started, 0.000001)
                self.result_ready.emit(people, vehicles, fps, task_id, objects, frame)
            except Exception as exc:
                self.error.emit(f"영상 AI 오류: {exc}")
                tracker, previous_task = None, None
                with self.condition:
                    self.stopping = True  # 같은 오류를 매 프레임 반복하지 않음
                    self.pending = None
                return


def fitted_zoom_rect(roi, frame_width, frame_height, view_width, view_height):
    x1, y1, x2, y2 = roi
    x1, x2 = sorted((max(0, min(frame_width, x1)), max(0, min(frame_width, x2))))
    y1, y2 = sorted((max(0, min(frame_height, y1)), max(0, min(frame_height, y2))))
    width, height = x2-x1, y2-y1
    if width <= 0 or height <= 0 or view_width <= 0 or view_height <= 0:
        return None
    ratio = view_width/view_height  # 확대칸과 같은 비율로 주변 영상까지 포함
    crop_width, crop_height = max(width, height*ratio), max(height, width/ratio)
    if crop_width > frame_width or crop_height > frame_height:
        return (int(x1), int(y1), min(frame_width, int(x2+0.999)), min(frame_height, int(y2+0.999)))  # 원본 밖은 여백으로 표시
    left = max(0, min(frame_width-crop_width, (x1+x2-crop_width)/2))
    top = max(0, min(frame_height-crop_height, (y1+y2-crop_height)/2))
    return (int(left), int(top), min(frame_width, int(left+crop_width+0.999)), min(frame_height, int(top+crop_height+0.999)))

class ZoomView(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.source_frame = None
        self.source_roi = None

    def show_frame(self, frame, roi):
        self.source_frame = frame.copy()  # 원본을 보관해 창 크기가 바뀌어도 다시 맞춤
        self.source_roi = tuple(roi)
        self.update()

    def clear(self):
        self.source_frame = None
        self.source_roi = None
        super().clear()

    def setText(self, text):
        self.source_frame = None
        self.source_roi = None
        super().setText(text)

    def paintEvent(self, event):
        if self.source_frame is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0B1420"))
        area = self.contentsRect()
        painter.setClipRect(area)  # 이벤트 목록과 겹치지 않게 확대칸 안에서만 그림
        height, width = self.source_frame.shape[:2]
        bounds = fitted_zoom_rect(self.source_roi, width, height, area.width(), area.height())
        if bounds is None:
            return
        x1, y1, x2, y2 = bounds
        crop = self.source_frame[y1:y2, x1:x2]
        if not crop.size:
            return
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image).scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        left = area.x()+(area.width()-pixmap.width())//2
        top = area.y()+(area.height()-pixmap.height())//2
        painter.drawPixmap(left, top, pixmap)  # 대상 전체와 주변 영상을 비율에 맞춰 표시

class VideoLabel(QLabel):
    region_selected = Signal(QRect)  # 기존 줄
    target_clicked = Signal(QPoint)  # 대상을 클릭한 화면 좌표 전달

    def __init__(self, text):
        super().__init__(text)
        self.zoom_enabled = False
        self.select_enabled = False                     # False: 대상 선택 모드 꺼짐
        self.zone_enabled = False
        self.zone_points = []                           # 작성 중인 구역의 점
        self.zones = []                                 # 완성한 구역 목록
        self.selected_zone = None                       # 삭제할 구역 번호, None은 선택 없음
        self.zone_level = "위험"                         # 새 구역에 적용할 등급
        self.moving_point = None                        # 이동 중인 구역과 점 번호
        self.drag_start = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)

    def set_select_mode(self, enabled):
        self.select_enabled = enabled  # True면 클릭으로 대상 선택
        self.drag_start = None  # 진행 중인 확대 드래그 해제
        self.rubber_band.hide()  # 드래그 테두리 숨기기
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)

    def set_zoom_mode(self, enabled):
        self.zoom_enabled = enabled
        self.drag_start = None
        self.moving_point = None
        self.rubber_band.hide()
        active = self.zoom_enabled or self.zone_enabled
        self.setCursor(Qt.CursorShape.CrossCursor if active else Qt.CursorShape.ArrowCursor)

    def set_zone_level(self, level):
        self.zone_level = level                         # 완성한 구역의 등급은 유지
        self.update()

    def set_zone_mode(self, enabled):
        self.zone_enabled = enabled
        self.drag_start = None
        self.moving_point = None
        self.rubber_band.hide()
        active = self.zoom_enabled or self.zone_enabled
        self.setCursor(Qt.CursorShape.CrossCursor if active else Qt.CursorShape.ArrowCursor)
        if enabled:
            self.setFocus()                             # Enter와 Esc 입력 받기

    def image_rect(self):
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        area = self.contentsRect()
        pw, ph = pixmap.width(), pixmap.height()
        left = area.x() + (area.width() - pw) // 2
        top = area.y() + (area.height() - ph) // 2
        return QRect(left, top, pw, ph)                  # 실제 영상이 표시되는 영역

    def keyPressEvent(self, event):
        if self.zone_enabled and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):  # 엔터·스페이스로 구역 완성
            if len(self.zone_points) >= 3:
                self.zones.append({
                    "points": self.zone_points.copy(),  # 현재 점 목록 보관
                    "level": self.zone_level            # 현재 등급 함께 보관
                })
                self.zone_points.clear()                # 다음 구역을 작성할 준비
                self.moving_point = None
                self.update()
            return

        if self.zone_enabled and event.key() == Qt.Key.Key_Escape:
            self.zone_points.clear()                    # 작성 중인 구역만 취소
            self.moving_point = None
            self.update()
            return

        if self.zone_enabled and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):  # 선택 구역 삭제
            if self.selected_zone is not None:
                self.zones.pop(self.selected_zone)  # 선택한 구역만 제거
                self.selected_zone = None  # 선택 초기화
                self.moving_point = None  # 점 이동 초기화
                self.update()  # 화면 다시 그리기
            return

        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.image_rect()
        if rect is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = {"주의": "#FFD54F", "경고": "#FF9800", "위험": "#FF5252"}

        def draw_zone(saved_points, level, closed, selected=False):  # 선택 여부 받기
            points = [
                QPointF(rect.x() + p.x() * rect.width(),
                        rect.y() + p.y() * rect.height())
                for p in saved_points
            ]
            if not points:
                return

            color = QColor(colors[level])
            color.setAlpha(130)                         # 점과 선의 투명도

            if closed and len(points) >= 3:
                fill_color = QColor(color)
                fill_color.setAlpha(35)                 # 내부는 옅게 채우기
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill_color)
                painter.drawPolygon(QPolygonF(points))

            painter.setPen(QPen(color, 2 if selected else 1))  # 선택한 구역만 선을 굵게
            for i in range(1, len(points)):
                painter.drawLine(points[i - 1], points[i])
            if closed and len(points) >= 3:
                painter.drawLine(points[-1], points[0])

            painter.setBrush(color)
            for point in points:
                painter.drawEllipse(point, 3, 3)

        for i, zone in enumerate(self.zones):  # 구역 번호와 내용 가져오기
            draw_zone(zone["points"], zone["level"], True, i == self.selected_zone)  # 선택 구역 표시

        draw_zone(self.zone_points, self.zone_level, False) # 작성 중인 구역 표시
        painter.end()

    def mousePressEvent(self, event):
        if self.select_enabled and event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()  # 마우스로 누른 위치
            rect = self.image_rect()  # 화면에서 실제 영상이 있는 영역
            if rect is not None and rect.contains(point):
                self.target_clicked.emit(point)  # 영상 내부 클릭만 전달
            return  # 대상 선택 중에는 구역·확대 드래그 처리하지 않기    

        rect = self.image_rect()

        if self.zone_enabled and event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            if rect is None:
                return
            pos = event.position()

            groups = [(None, self.zone_points)]
            groups += [(i, zone["points"]) for i, zone in enumerate(self.zones)]

            for zone_index, points in reversed(groups):
                for point_index, point in enumerate(points):
                    px = rect.x() + point.x() * rect.width()
                    py = rect.y() + point.y() * rect.height()
                    if (pos.x() - px) ** 2 + (pos.y() - py) ** 2 <= 100:
                        self.moving_point = (zone_index, point_index)
                        self.selected_zone = zone_index  # 클릭한 구역 선택
                        self.update()  # 선택 표시 즉시 반영
                        return

            x = (pos.x() - rect.x()) / rect.width()
            y = (pos.y() - rect.y()) / rect.height()
            if 0 <= x <= 1 and 0 <= y <= 1:
                self.zone_points.append(QPointF(x, y))  # 다음 구역의 점 추가
                self.update()
            return

        if self.zoom_enabled and event.button() == Qt.MouseButton.LeftButton and rect is not None:
            self.drag_start = event.position().toPoint()
            self.rubber_band.setGeometry(QRect(self.drag_start, self.drag_start))
            self.rubber_band.show()

    def mouseMoveEvent(self, event):
        if self.moving_point is not None:
            rect = self.image_rect()
            if rect is None:
                return
            pos = event.position()
            x = max(0.0, min(1.0, (pos.x() - rect.x()) / rect.width()))
            y = max(0.0, min(1.0, (pos.y() - rect.y()) / rect.height()))
            zone_index, point_index = self.moving_point

            points = self.zone_points if zone_index is None else self.zones[zone_index]["points"]
            points[point_index] = QPointF(x, y)          # 선택한 구역의 점 이동
            self.update()
            return

        if self.drag_start is not None:
            rect = QRect(self.drag_start, event.position().toPoint()).normalized()
            self.rubber_band.setGeometry(rect.intersected(self.contentsRect()))

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self.moving_point is not None:
            self.moving_point = None                     # 점 이동 종료
            return

        if self.drag_start is not None:
            rect = QRect(self.drag_start, event.position().toPoint()).normalized()
            rect = rect.intersected(self.contentsRect())
            self.drag_start = None
            self.rubber_band.hide()
            if rect.width() >= 10 and rect.height() >= 10:
                self.region_selected.emit(rect)         # 확대 영역 전달
                
app = QApplication(sys.argv)  # 프로그램 실행에 필요한 UI 환경 생성
window = QMainWindow()  # 메인 창 생성
window.setWindowTitle("드론 안전 관제")  # 창 상단 제목
window.resize(1400, 850)  # 처음 열리는 창 크기: 가로, 세로
window.setMinimumSize(1000, 650)  # 창을 줄일 수 있는 최소 크기

fullscreen_geometry = None  # 전체 화면으로 들어가기 전 창 위치와 크기
fullscreen_was_maximized = False

def sync_fullscreen_button():
    fullscreen_button.setText("창으로 복귀" if window.isFullScreen() else "전체 화면")
    fullscreen_button.setToolTip("전체 화면 해제 · Esc / F11" if window.isFullScreen() else "모니터 전체 화면 · F11")

def leave_fullscreen():
    if not window.isFullScreen():
        return
    window.showNormal()
    if fullscreen_geometry is not None:
        window.restoreGeometry(fullscreen_geometry)  # 이전 창 위치와 크기 복원
    if fullscreen_was_maximized:
        window.showMaximized()
    sync_fullscreen_button()
    QTimer.singleShot(0, refresh_panes)  # 복귀한 영상칸 크기에 맞춰 다시 그림

def toggle_fullscreen():
    global fullscreen_geometry, fullscreen_was_maximized
    if window.isFullScreen():
        leave_fullscreen()
    else:
        fullscreen_geometry = window.saveGeometry()
        fullscreen_was_maximized = window.isMaximized()
        window.showFullScreen()  # 분할 구성은 유지하고 창 전체만 확대
        sync_fullscreen_button()
        QTimer.singleShot(0, refresh_panes)

window.setStyleSheet("""QMainWindow {background-color: #0B1420;}QLabel {color: #E8F0FA;font-size: 28px;font-weight: bold;}""")  # 배경색과 글자 모양 설정

container = QWidget()
root_layout = QVBoxLayout(container)                     # 상단 제목과 본문을 세로 배치
root_layout.setContentsMargins(16, 16, 16, 16)
root_layout.setSpacing(12)

header = QFrame()
header.setStyleSheet("background: #111F2E; border-radius: 10px;")
header_layout = QHBoxLayout(header)
header_layout.setContentsMargins(18, 12, 18, 12)

app_title = QLabel("드론 안전 관제")
app_title.setStyleSheet("color: #E8F0FA; font-size: 26px; font-weight: bold;")

subtitle = QLabel("AI SAFETY MONITOR · 4분할 v12")
subtitle.setStyleSheet("color: #91A7BD; font-size: 12px; font-weight: normal;")

status_label = QLabel("● 대기 중")
status_label.setStyleSheet("""
    color: #2EDDB5;
    background: #12313A;
    border: 1px solid #20606C;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 14px;
""")

header_layout.addWidget(app_title)
header_layout.addWidget(subtitle)
header_layout.addStretch()                               # 상태 표시를 오른쪽으로 밀기
header_layout.addWidget(status_label)
alert_label = QLabel("● 감지 대기")  # 상단에 표시할 안전 상태
alert_label.setStyleSheet("color: #91A7BD; background: #182C40; border-radius: 8px; padding: 8px 16px; font-size: 14px;")
header_layout.addWidget(alert_label)  # 재생 상태 오른쪽에 추가
fullscreen_button = QPushButton("전체 화면")  # 상단 오른쪽의 전체 화면 버튼
fullscreen_button.setMinimumHeight(38)
fullscreen_button.setStyleSheet("background:#007F99; color:#E8F0FA; border:1px solid #159BB0; border-radius:8px; padding:8px 14px; font-size:14px; font-weight:bold;")
fullscreen_button.clicked.connect(toggle_fullscreen)
header_layout.addWidget(fullscreen_button)
fullscreen_shortcut = QShortcut(QKeySequence("F11"), window)  # 키보드로도 전환 가능
fullscreen_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
fullscreen_shortcut.activated.connect(toggle_fullscreen)
sync_fullscreen_button()
root_layout.addWidget(header)

content = QWidget()
layout = QHBoxLayout(content)                            # 기존 세 구역을 담을 본문
layout.setContentsMargins(0, 0, 0, 0)
layout.setSpacing(12)
root_layout.addWidget(content, 1)                        # 남은 공간을 본문에 배정

left_panel = QFrame()  # 제목·버튼·목록을 담을 왼쪽 영역
left_panel.setObjectName("library")
left_panel.setStyleSheet("""
    QFrame#library {background-color: #111F2E; border: 1px solid #263B50; border-radius: 10px;}
    QLabel {font-size: 18px; color: #DCE6F2;}
    QPushButton {background-color: #167D96; color: white; border: none; border-radius: 6px; padding: 10px;}
    QPushButton:hover {background-color: #209BB8;}
    QListWidget {background-color: #0B1420; color: #DCE6F2; border: none; border-radius: 6px; font-size: 14px;}
""")
left_layout = QVBoxLayout(left_panel)  # 내용을 위에서 아래로 배치
left_layout.setContentsMargins(12, 16, 12, 12)  # 영역 안쪽 여백
left_layout.setSpacing(12)  # 항목 사이 간격

library_title = QLabel("영상 라이브러리")       # 상단 제목
add_button = QPushButton("+ 영상 추가")       # 영상 선택 버튼
stream_button = QPushButton("+ 실시간 연결")  # 스트리밍 주소 입력 버튼
video_list = QListWidget()                 # 추가한 영상 이름을 표시할 목록

left_layout.addWidget(library_title)  # 제목 배치
left_layout.addWidget(add_button)     # 버튼 배치
left_layout.addWidget(video_list, 1)  # 남은 높이를 목록으로 채우기

stream_panel = QFrame()  # 실시간 연결 전용 영역
stream_panel.setObjectName("streamPanel")
stream_panel.setStyleSheet("""
    QFrame#streamPanel {
        background: #111F2E;
        border: 1px solid #263B50;
        border-radius: 8px;
    }
""")
stream_layout = QVBoxLayout(stream_panel)  # 내용을 위아래로 배치
stream_layout.setContentsMargins(12, 16, 12, 16)
stream_layout.setSpacing(12)

stream_title = QLabel("실시간 스트림")  # 영역 제목
stream_hint = QLabel("스트림 URL (RTSP / HTTP)")
stream_hint.setStyleSheet("color: #91A7BD; font-size: 12px;")

stream_url = QLineEdit()  # 연결할 주소를 입력하는 칸
stream_url.setPlaceholderText("rtsp://192.168.0.10:554/stream1")  # 입력 예시
stream_url.setMinimumHeight(40)
stream_url.setStyleSheet("background: #0B1420; color: #DCE6F2; border: 1px solid #36536C; border-radius: 6px; padding: 6px; font-size: 13px;")

stream_button.setText("연결하기")  # 기존 버튼의 이름 변경
stream_button.setMinimumHeight(42)

stream_layout.addWidget(stream_title)
stream_layout.addWidget(stream_hint)
stream_layout.addWidget(stream_url)
stream_layout.addWidget(stream_button)
left_layout.addWidget(stream_panel)  # 영상 목록 아래에 전용 영역 추가

def add_videos():  # 선택한 영상을 목록에 추가하는 함수
    paths, _ = QFileDialog.getOpenFileNames(window, "영상 선택", "videos", "영상 파일 (*.mp4 *.mov *.avi *.mkv *.webm)")
    existing = {video_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(video_list.count())}  # 등록된 경로 모음
    for path in paths:  # 선택한 파일을 하나씩 처리
        if path in existing:  # 이미 등록된 영상이면 건너뛰기
            continue
        item = QListWidgetItem(Path(path).name)            # 목록에는 파일 이름만 표시
        item.setData(Qt.ItemDataRole.UserRole, path)       # 재생에 사용할 전체 경로 보관
        item.setToolTip(path)                              # 마우스를 올리면 전체 경로 표시
        video_list.addItem(item)                            # 목록에 항목 추가
        existing.add(path)                                 # 중복 확인용 경로 모음에도 추가

add_button.clicked.connect(add_videos)                    # 버튼을 클릭하면 함수 실행

def add_stream():
    url = stream_url.text().strip()                   # 입력칸의 주소 가져오기
    if not url:                                       # 주소가 비어 있으면 입력칸으로 이동
        stream_url.setFocus()
        return
    for i in range(video_list.count()):               # 이미 등록한 주소인지 확인
        item = video_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == url:
            video_list.setCurrentItem(item)           # 기존 항목 선택
            return
    item = QListWidgetItem("실시간 스트리밍")             # 목록에 표시할 이름
    item.setData(Qt.ItemDataRole.UserRole, url)       # 연결 주소 보관
    item.setData(Qt.ItemDataRole.UserRole + 1, True)  # True: 실시간 영상
    item.setToolTip(url)  # 마우스를 올리면 주소 표시
    video_list.addItem(item)
    video_list.setCurrentItem(item)

stream_button.clicked.connect(add_stream)  # 연결하기 버튼에 함수 연결
stream_url.returnPressed.connect(add_stream)  # 입력칸에서 Enter도 사용

video_panel = VideoLabel("관제 영상\n\n영상을 선택해 주세요")    # 가운데: 영상 표시 자리
right_panel = QFrame()                                    # 오른쪽 확대 화면과 이벤트 목록 공간
right_panel.setObjectName("rightPanel")
right_panel.setStyleSheet("""
    QFrame#rightPanel { background: #111F2E; border: 1px solid #263B50; border-radius: 10px; }
    QLabel { color: #DCE6F2; font-size: 18px; }
    QListWidget { background: #0B1420; color: #DCE6F2; border: none; border-radius: 6px; font-size: 14px; }
""")
right_layout = QVBoxLayout(right_panel)  # 오른쪽 요소를 위아래로 배치
right_layout.setContentsMargins(12, 16, 12, 12)
right_layout.setSpacing(12)

zoom_title = QLabel("확대 화면")  # 확대 영역 제목
zoom_panel = ZoomView("확대할 영역을 선택해 주세요")  # 확대 영상을 표시할 자리
zoom_panel.setAlignment(Qt.AlignmentFlag.AlignCenter)
zoom_panel.setMinimumSize(1, 1)
zoom_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
zoom_panel.setStyleSheet("background: #0B1420; border-radius: 6px; color: #91A7BD; font-size: 14px;")

event_title = QLabel("이벤트 기록")  # 경고 목록 제목
zoom_name = QLabel(zoom_panel)  # 확대 화면 안에 이름 표시
zoom_name.setStyleSheet("color: white; background: rgba(0, 0, 0, 150); padding: 6px 10px; border-radius: 5px; font-weight: bold;")
zoom_name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)  # 마우스 클릭 방해 방지
zoom_name.move(12, 12)  # 확대 화면 왼쪽 위에 고정
zoom_name.hide()  # 대상 선택 전에는 숨김
event_list = QListWidget()  # 감지된 경고를 표시할 목록

right_layout.addWidget(zoom_title)
right_layout.addWidget(zoom_panel, 1)  # 확대창과 이벤트 목록을 일정한 비중으로 배치
right_layout.addWidget(event_title)
right_layout.addWidget(event_list, 1)  # 아래쪽 이벤트 목록

for panel in (video_panel,):  # 가운데 영상에만 기존 디자인 적용
    panel.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 글자 가운데 정렬
    panel.setStyleSheet("""
        background-color: #111F2E;
        border: 1px solid #263B50;
        border-radius: 10px;
        color: #DCE6F2;
        font-size: 18px;
    """)
video_panel.setMinimumSize(1, 1)  # 큰 영상 때문에 창이 늘어나는 것 방지
video_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
cap = None                         # 현재 열려 있는 영상
stream_capture = None              # 현재 실시간 영상 작업, None은 연결 없음
stream_capture_jobs = set()        # 종료될 때까지 영상 작업을 보관
last_frame = None                  # 현재 화면의 원본 영상 보관
last_raw_frame = None              # 박스 없는 확대용 원본 영상
zoom_roi = None                    # 확대할 영역의 원본 좌표 보관
timer = QTimer(window)             # 영상 화면을 갱신할 타이머
ai_worker = AIWorker(window)       # AI 작업 스레드 준비
ai_task_id = 0                     # 영상이 바뀌었을 때 이전 분석 결과를 구분
tracked_objects = []               # 현재 화면에서 선택할 수 있는 대상 목록
selected_track_id = None           # 선택한 대상 ID, 처음에는 선택 없음
zone_previous = set()              # 이전 분석에서 구역 안에 있던 사람
zone_last_logged = {}              # 사람·구역별 마지막 경고 기록 시간
ppe_last_logged = {}               # 사람 ID와 미착용 항목별 마지막 기록 시간
ppe_warning_until = 0.0            # 보호구 경고가 끝나는 시간
fall_warning_until = 0.0           # 넘어짐 위험 표시가 끝나는 시간
fall_last_logged = {}              # 사람 ID별 마지막 넘어짐 기록 시간
zone_alert_rank = 0                # 구역 상태: 정상 0, 주의 1, 경고 2, 위험 3


def save_zone_event(track_id, zone_number, level):
    path = Path(__file__).resolve().parent / "data" / "ui_zone_events.csv"  # main.py 기준 저장 위치
    path.parent.mkdir(parents=True, exist_ok=True)  # data 폴더가 없으면 생성
    with path.open("a", newline="", encoding="utf-8-sig") as file:  # 기존 기록 뒤에 추가
        writer = csv.writer(file)
        if file.tell() == 0:
            writer.writerow(["time", "track_id", "zone_number", "level"])  # 처음에만 항목 이름 저장
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), track_id, zone_number, level])

def update_safety_alert():
    if cap is None and stream_capture is None:
        return  # 영상이 없으면 기존 대기 표시 유지

    ppe_rank = 2 if monotonic() < ppe_warning_until else 0  # 보호구 경고는 10초 유지
    fall_rank = 3 if monotonic() < fall_warning_until else 0  # 넘어짐 의심은 위험 등급
    rank = max(zone_alert_rank, ppe_rank, fall_rank)  # 가장 높은 등급 표시
    level = {0: "정상", 1: "주의", 2: "경고", 3: "위험"}[rank]
    color = {0: "#2EDDB5", 1: "#FACC15", 2: "#FB923C", 3: "#F87171"}[rank]

    alert_label.setText(f"● {level}")
    alert_label.setStyleSheet(
        f"color: {color}; background: #182C40; border-radius: 8px; "
        "padding: 8px 16px; font-size: 14px;"
    )
alert_timer = QTimer(window)  # 일시정지 중에도 경고 종료 시간을 확인
alert_timer.timeout.connect(update_safety_alert)
alert_timer.start(250)  # 0.25초마다 상태 갱신

def check_zone_intrusion(objects, frame, source=0):
    global zone_previous, zone_alert_rank
    height, width = frame.shape[:2]  # 원본 영상의 높이와 너비
    current = set()  # 현재 구역 안에 있는 사람
    highest = 0  # 현재 감지된 가장 높은 등급
    ranks = {"주의": 1, "경고": 2, "위험": 3}
    colors = {"주의": "#FACC15", "경고": "#FB923C", "위험": "#F87171"}
    now = monotonic()

    panel = video_panels[source]
    previous = zone_states[source]
    for obj in objects:
        if obj["name"] != "사람" or obj["id"] < 0:
            continue  # 차량과 추적 번호 없는 대상은 제외
        x1, y1, x2, y2 = obj["bbox"]
        foot = QPointF((x1 + x2) / 2 / width, y2 / height)  # 발 위치를 구역과 같은 비율 좌표로 변환

        for number, zone in enumerate(panel.zones, start=1):
            polygon = QPolygonF(zone["points"])  # 저장된 점으로 다각형 생성
            if not polygon.containsPoint(foot, Qt.FillRule.OddEvenFill):
                continue  # 발이 구역 밖이면 다음 구역 확인
            level = zone["level"]
            highest = max(highest, ranks[level])  # 여러 구역 중 가장 높은 등급 선택
            key = (source, id(zone), obj["id"])  # 구역과 사람을 함께 구분
            current.add(key)

            if key not in previous and now - zone_last_logged.get(key, -10) >= 10:
                text = f"{datetime.now():%H:%M:%S}  [{level}] {source+1}번 구역 {number} · 사람 ID {obj['id']} 진입"
                item = QListWidgetItem(text)
                item.setForeground(QColor(colors[level]))  # 구역 등급에 맞는 글자 색
                event_list.insertItem(0, item)  # 최신 경고를 위에 표시
                save_zone_event(obj["id"], number, level)  # 화면에 추가한 이벤트를 CSV에도 저장
                zone_last_logged[key] = now  # 마지막 기록 시간 갱신
                if event_list.count() > 200:
                    event_list.takeItem(event_list.count() - 1)  # 오래된 기록부터 제거

    zone_states[source] = current
    zone_ranks[source] = highest
    zone_previous = current
    zone_alert_rank = max(zone_ranks)  # 현재 구역 침입의 최고 등급 저장
    update_safety_alert()  # 보호구 경고와 함께 상단 표시 갱신

def show_fall_result(falls, task_id):
    global fall_warning_until

    if task_id != ai_task_id or (cap is None and stream_capture is None):
        return  # 이전 영상의 결과 무시

    now = monotonic()
    if falls:
        fall_warning_until = now + 4  # 넘어짐 의심 감지 시 위험 표시 10초 유지
    update_safety_alert()

    path = Path(__file__).resolve().parent / "data" / "fall_events.csv"
    path.parent.mkdir(parents=True, exist_ok=True)  # 저장 폴더 준비

    for track_id in falls:
        if now - fall_last_logged.get(track_id, -10) < 10:
            continue  # 같은 ID는 10초 안에 중복 기록하지 않음

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item = QListWidgetItem(
            f"{timestamp[11:]}  [위험] 사람 ID {track_id} · 넘어짐 의심"
        )
        item.setForeground(QColor("#F87171"))  # 위험 기록은 빨간색
        event_list.insertItem(0, item)  # 최신 기록을 맨 위에 표시

        with path.open("a", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(["time", "track_id"])  # 빈 파일에 제목 추가
            writer.writerow([timestamp, track_id])  # 기존 CSV 뒤에 기록 추가

        fall_last_logged[track_id] = now  # 마지막 기록 시간 갱신
        if event_list.count() > 200:
            event_list.takeItem(event_list.count() - 1)  # 화면 기록 최대 200개

def show_ppe_result(violations, task_id):
    global ppe_warning_until  # 함수 밖에 있는 경고 종료 시간을 변경

    if task_id != ai_task_id or (cap is None and stream_capture is None):
        return  # 이전 영상의 결과 무시

    now = monotonic()  # 현재 시간
    if violations:
        ppe_warning_until = now + 4  # 미착용 감지 시 경고를 10초 연장
    update_safety_alert()  # 상단 경고 표시 갱신

    names = {"NO_HARDHAT": "안전모 미착용", "NO_SAFETY_VEST": "안전조끼 미착용"}
    path = Path(__file__).resolve().parent / "data" / "ppe_events.csv"
    path.parent.mkdir(parents=True, exist_ok=True)  # 저장 폴더가 없으면 생성

    for violation in violations:
        track_id = violation["track_id"]  # 미착용이 감지된 사람 ID
        missing_item = violation["missing_item"]  # 미착용 보호구 종류
        key = (track_id, missing_item)  # 사람과 보호구를 함께 구분

        if now - ppe_last_logged.get(key, -10) < 10:
            continue  # 같은 사람의 같은 항목은 10초 안에 다시 기록하지 않음

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{timestamp[11:]}  [경고] 사람 ID {track_id} · {names[missing_item]}"
        item = QListWidgetItem(text)
        item.setForeground(QColor("#FB923C"))  # 경고 기록은 주황색
        event_list.insertItem(0, item)  # 최신 기록을 맨 위에 표시

        with path.open("a", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(["time", "track_id", "missing_item"])  # 빈 파일에 제목 추가
            writer.writerow([timestamp, track_id, missing_item])  # 기존 기록 뒤에 추가

        ppe_last_logged[key] = now  # 마지막 기록 시간 갱신
        if event_list.count() > 200:
            event_list.takeItem(event_list.count() - 1)  # 화면 기록은 최대 200개

def show_ai_result(frame, people, vehicles, objects, task_id, raw_frame, ai_fps):
    global last_frame, tracked_objects, last_raw_frame
    if task_id != ai_task_id or (cap is None and stream_capture is None):
        return
    last_raw_frame = raw_frame                     # 같은 분석 결과의 원본 영상 보관
    tracked_objects = objects  # 현재 대상의 ID와 좌표 저장
    check_zone_intrusion(objects, raw_frame)  # 상단 안전 상태와 오른쪽 이벤트 갱신
    set_pane_stats(0, people, vehicles, ai_fps)  # 현재 분석 결과 표시
    last_frame = frame.copy()  # 확대에 사용할 분석 화면 저장
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # Qt용 색상 순서
    height, width = rgb.shape[:2]
    image = QImage(rgb.data, width, height, rgb.strides[0],
                   QImage.Format.Format_RGB888).copy()  # Qt 이미지 생성
    pixmap = QPixmap.fromImage(image)
    video_panel.setPixmap(pixmap.scaled(
        video_panel.contentsRect().size(),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation
    ))  # 분석 화면 표시
    update_zoom()  # 오른쪽 확대 화면 갱신

def show_ai_error(message):
    event_list.addItem(f"AI 오류: {message}")  # 오류를 이벤트 목록에 표시

ai_worker.result_ready.connect(show_ai_result)   # 분석 결과 받기
ai_worker.ppe_ready.connect(show_ppe_result)     # 보호구 검사 결과를 이벤트 기록에 연결
ai_worker.fall_ready.connect(show_fall_result)  # 넘어짐 결과를 UI에 연결
ai_worker.error.connect(show_ai_error)           # 오류 내용 받기

def stop_video():
    global cap, stream_capture, ai_task_id, ppe_warning_until, zone_alert_rank, fall_warning_until # 영상 연결과 작업 번호 사용
    ai_task_id += 1  # 이전 영상의 분석 결과 무효화
    timer.stop()
    if stream_capture is not None:
        stream_capture.stop()  # 실시간 영상 읽기 종료 요청
        stream_capture = None  # 현재 연결 해제

    stream_button.setEnabled(True)  # 연결 버튼 다시 사용 가능
    stream_button.setText("연결하기")
    video_current.setEnabled(True)  # 일반 영상의 시간 입력 다시 허용

    if selected_pane == 0:
        video_slider.setEnabled(False)  # 첫 영상 종료가 두 번째 시간 막대에 영향을 주지 않음
        status_label.setText("● 대기 중")
    for key in list(zone_last_logged):
        if key[0] == 0:
            zone_last_logged.pop(key)  # 첫 영상의 기록 간격만 초기화
    ppe_last_logged.clear()  # 영상 종료 시 보호구 기록 간격 초기화
    ppe_warning_until = 0.0  # 보호구 경고 종료
    fall_warning_until = 0.0  # 넘어짐 위험 표시 초기화
    fall_last_logged.clear()  # 넘어짐 중복 기록 시간 초기화
    zone_states[0].clear()
    zone_ranks[0] = 0
    zone_alert_rank = max(zone_ranks)  # 두 번째 화면의 구역 경고 유지
    alert_label.setText("● 감지 대기")
    alert_label.setStyleSheet("color: #91A7BD; background: #182C40; border-radius: 8px; padding: 8px 16px; font-size: 14px;")

    if cap is not None:
        cap.release()
        cap = None
def update_zoom():
    global zoom_roi
    if selected_pane > 0:
        extra_panes[selected_pane-1].update_zoom()
        return
    if last_raw_frame is None:
        return

    if selected_track_id is not None:
        target = next((obj for obj in tracked_objects
                       if obj["id"] == selected_track_id), None)  # 선택한 ID의 현재 위치 찾기
        if target is None:
            zoom_name.hide()  # 대상을 놓치면 이름표 숨김
            zoom_panel.clear()                                  # 대상을 놓치면 확대 화면 비우기
            zoom_panel.setText("선택 대상 확인 중")
            return
        zoom_name.setText(f"{target['name']} · ID {target['id']}")  # 대상 이름과 추적 ID
        zoom_name.adjustSize()  # 글자 길이에 맞춰 이름표 크기 조절
        zoom_name.show()  # 이름표 표시
        zoom_name.raise_()  # 영상 위에 표시
        height, width = last_raw_frame.shape[:2]
        x1, y1, x2, y2 = target["bbox"]
        pad = 20
        new_roi = (max(0, x1 - pad), max(0, y1 - pad),
                   min(width, x2 + pad), min(height, y2 + pad))  # 현재 대상의 확대 범위
        if zoom_roi is None:
            zoom_roi = new_roi  # 처음에는 바로 적용
        else:
            alpha = 0.2  # 작을수록 부드럽지만 따라가는 반응은 느려짐
            zoom_roi = tuple(old + (new - old) * alpha for old, new in zip(zoom_roi, new_roi))  # 위치와 크기를 서서히 변경
    if zoom_roi is None:
        return
    display_roi = zoom_roi
    if selected_track_id is not None:
        nx1, ny1, nx2, ny2 = new_roi
        rx1, ry1, rx2, ry2 = zoom_roi
        display_roi = (min(rx1,nx1), min(ry1,ny1), max(rx2,nx2), max(ry2,ny2))  # 이동 중에도 현재 대상 전체 포함
    render_zoom(last_raw_frame, display_roi)

def select_zoom(rect):  # 화면에서 선택한 영역을 원본 영상 좌표로 변환
    global zoom_roi, selected_track_id
    selected_track_id = None                         # 드래그 확대는 고정 영역을 사용
    zoom_name.hide()  # 드래그 확대에서는 대상 이름표 숨김
    zoom_title.setText("확대 화면")  # 제목 초기화
    pixmap = video_panel.pixmap()
    if last_frame is None or pixmap is None or pixmap.isNull():
        return  # 영상이 없으면 종료

    area = video_panel.contentsRect()  # 영상 패널의 내부 영역
    pw, ph = pixmap.width(), pixmap.height()  # 화면에 표시된 영상 크기
    left = area.x() + (area.width() - pw) // 2  # 영상 왼쪽 위치
    top = area.y() + (area.height() - ph) // 2  # 영상 위쪽 위치
    image_rect = QRect(left, top, pw, ph)  # 여백을 제외한 실제 영상 영역
    rect = rect.intersected(image_rect)  # 영상 밖으로 선택한 부분 제외
    if rect.width() < 10 or rect.height() < 10:
        return  # 선택 영역이 너무 작으면 종료

    height, width = last_frame.shape[:2]  # 원본 영상 크기
    x1 = max(0, int((rect.x() - left) * width / pw))
    y1 = max(0, int((rect.y() - top) * height / ph))
    x2 = min(width, int((rect.x() + rect.width() - left) * width / pw))
    y2 = min(height, int((rect.y() + rect.height() - top) * height / ph))
    zoom_roi = (x1, y1, x2, y2)  # 원본에서 자를 좌표 보관
    update_zoom()  # 일시정지 상태에서도 확대 화면 바로 표시

video_panel.region_selected.connect(select_zoom)  # 드래그 완료 신호 연결

def select_target(point):
    global zoom_roi, selected_track_id                   # 확대 영역과 선택한 ID를 변경
    rect = video_panel.image_rect()                      # 화면에 표시된 영상 영역
    if last_frame is None or rect is None:
        return

    height, width = last_frame.shape[:2]
    x = (point.x() - rect.x()) * width / rect.width()      # 클릭 위치를 원본 영상 좌표로 변환
    y = (point.y() - rect.y()) * height / rect.height()

    candidates = []
    for obj in tracked_objects:
        x1, y1, x2, y2 = obj["bbox"]
        if x1 <= x <= x2 and y1 <= y <= y2:
            candidates.append(obj)                       # 클릭한 위치에 있는 대상 저장

    if not candidates:
        return

    target = min(candidates, key=lambda obj:             # 박스가 겹치면 작은 대상을 선택
                 (obj["bbox"][2] - obj["bbox"][0]) *
                 (obj["bbox"][3] - obj["bbox"][1]))
    selected_track_id = target["id"]                    # 클릭한 대상의 ID를 기억
    x1, y1, x2, y2 = target["bbox"]
    pad = 20                                            # 대상 주변에 여백 추가
    zoom_roi = (max(0, x1 - pad), max(0, y1 - pad),
                min(width, x2 + pad), min(height, y2 + pad))
    zoom_title.setText(f"선택 대상 · ID {target['id']}")    # 확대 화면 위에 대상 ID 표시
    update_zoom()                                       # 오른쪽 확대 화면 갱신


video_panel.target_clicked.connect(select_target)        # 대상 클릭과 함수 연결

def update_frame():  # 최신 영상 한 장을 AI에 전달
    global last_frame

    if stream_capture is not None:
        frame = stream_capture.take_frame()  # 기다리지 않고 최신 영상 가져오기
        if frame is not None:
            ai_worker.submit(frame, ai_task_id)  # 실시간 영상도 기존 AI로 분석
        return  # 실시간 영상은 일반 파일 읽기를 실행하지 않기

    if cap is None:
        return
    ok, frame = cap.read()  # ok: 읽기 성공 여부, frame: 영상 한 장
    if not ok:  # 영상이 끝났거나 읽지 못하면 중지
        timer.stop()  # 끝난 뒤에도 시간 막대로 장면 이동 가능
        if selected_pane == 0:
            status_label.setText("● 1번 영상 끝 · 시간 이동 가능")
        return
    
    ai_worker.submit(frame, ai_task_id)  # 현재 영상을 AI에 전달

    if selected_pane == 0 and not video_slider.isSliderDown():  # 선택한 화면의 시간만 표시
        position = max(0, int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1)  # 현재 프레임
        video_slider.setValue(position)  # 슬라이더 위치 갱신
        fps = cap.get(cv2.CAP_PROP_FPS)  # 영상의 초당 프레임 수
        if fps > 0:
            current = int(position / fps)  # 현재 위치를 초로 변환
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps)  # 전체 영상 길이
            if not video_current.hasFocus():  # 입력 중에는 현재 시간을 덮어쓰지 않음
                video_current.setText(f"{current // 60:02d}:{current % 60:02d}")
            video_time.setText(f"/ {total // 60:02d}:{total % 60:02d}")  # 전체 시간
def play_video(item):
    select_pane(0)  # 목록의 영상은 첫 번째 관제 화면에서 재생
    global cap, last_frame, zoom_roi  # 영상과 확대 영역 변수 사용
    stop_video()  # 이전 영상 연결 해제
    last_frame = None  # 이전 원본 프레임 초기화
    zoom_roi = None  # 이전 확대 좌표 초기화
    zoom_panel.setText("확대할 영역을 선택해 주세요")  # 확대 이미지 대신 안내 표시
    zoom_button.setChecked(False)  # 확대 선택 모드 끄기
    path = item.data(Qt.ItemDataRole.UserRole)  # 목록에 보관한 전체 경로
    cap = cv2.VideoCapture(path)  # 영상 파일 열기
    if not cap.isOpened():
        stop_video()
        video_panel.setText("영상을 열 수 없습니다.")
        return
    fps = cap.get(cv2.CAP_PROP_FPS)  # 영상의 초당 프레임 수
    fps = fps if 1 <= fps <= 120 else 30  # FPS 정보가 잘못되면 30 사용
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))  # 전체 프레임 수
    video_slider.setRange(0, max(0, total_frames - 1))  # 이동 범위 설정
    video_slider.setValue(0)  # 시작 위치 초기화
    video_slider.setEnabled(total_frames > 1)  # 이동할 장면이 있으면 활성화
    window.setWindowTitle(f"드론 안전 관제 · {item.text()}")  # 재생 중인 파일 이름 표시
    video_title.setText(f"관제 영상 · {item.text()}")  # 선택한 파일 이름 표시
    update_frame()  # 첫 화면 바로 표시
    if cap is not None:
        timer.start(max(1, round(1000 / fps)))    # FPS에 맞춰 반복 갱신
        status_label.setText("● 재생 중")  # 새 영상을 시작할 때 표시

timer.timeout.connect(update_frame)               # 타이머가 울리면 다음 프레임 표시
video_list.itemClicked.connect(lambda item: open_library_video(item))  # 한 번 클릭하면 선택한 화면에 영상 열기
def shutdown():
    stop_video()  # 현재 영상과 실시간 작업 정지 요청
    for job in list(stream_capture_jobs):
        job.stop()  # 남아 있는 영상 작업에도 종료 요청
    for job in list(stream_jobs):
        job.wait()  # 주소 추출 작업 종료 기다리기
    for job in list(stream_capture_jobs):
        job.wait()  # 영상 연결이 해제될 때까지 기다리기
    for pane in extra_panes:
        pane.stop()
        pane.worker.stop()
    for pane in extra_panes:
        pane.worker.wait()  # 모든 영상 분석 종료까지 기다림
    ai_worker.stop()  # AI 종료 요청
    ai_worker.wait()  # AI 작업 종료 기다리기
app.aboutToQuit.connect(shutdown)  # 프로그램 종료 시 안전하게 정리
layout.addWidget(left_panel, 2)                   # 왼쪽 너비 비중
center_panel = QWidget()                          # 가운데 영상과 버튼을 담을 공간
center_layout = QVBoxLayout(center_panel)         # 영상과 버튼을 위아래로 배치
center_layout.setContentsMargins(0, 0, 0, 0)      # 바깥 여백 제거
center_layout.setSpacing(12)                      # 영상과 버튼 사이 간격
zoom_button = QPushButton("확대 영역 선택")          # 드래그 확대 모드 버튼
zoom_button.setCheckable(True)                    # 클릭할 때마다 켜짐과 꺼짐 전환
zoom_button.toggled.connect(video_panel.set_zoom_mode)  # 확대 버튼과 드래그 모드 연결
zoom_button.setMinimumHeight(40)
zoom_button.setStyleSheet("""
    QPushButton { background: #182C40; color: #DCE6F2; border: 1px solid #263B50;
                  border-radius: 6px; font-size: 14px; }
    QPushButton:hover { background: #23415B; }
    QPushButton:checked { background: #167D96; color: white; border-color: #209BB8; }
""")
zone_level = QComboBox()                          # 선택한 위험 등급을 담는 메뉴
zone_level.addItems(["주의", "경고", "위험"])        # 메뉴에 표시할 세 가지 등급
zone_level.setCurrentText("위험")                  # 처음에는 위험 등급을 선택
zone_level.currentTextChanged.connect(video_panel.set_zone_level)  # 선택 메뉴와 구역 색 연결
zone_level.setMinimumHeight(40)                   # 메뉴의 최소 높이
zone_level.setStyleSheet("background: #182C40; color: #DCE6F2; border: 1px solid #263B50; border-radius: 6px; padding: 6px;")
zone_button = QPushButton("＋ 구역 추가")
zone_button.setCheckable(True)
zone_button.setMinimumHeight(40)
zone_button.setStyleSheet(zoom_button.styleSheet())

select_button = QPushButton("SELECT · 대상 선택")
select_button.setCheckable(True)  # 누르면 켜지고 다시 누르면 꺼짐
select_button.setMinimumHeight(40)
select_button.setStyleSheet(zoom_button.styleSheet())  # 확대 버튼과 같은 디자인


def toggle_select(enabled):
    if enabled:
        zoom_button.setChecked(False)  # 영역 확대 끄기
        zone_button.setChecked(False)  # 구역 추가 끄기
    for index, panel in enumerate(video_panels):
        panel.set_select_mode(enabled and selected_pane == index)

def toggle_zone(enabled):
    if enabled:
        select_button.setChecked(False)  # 대상 선택 끄기
        zoom_button.setChecked(False)  # 영역 확대 끄기
    for index, panel in enumerate(video_panels):
        panel.set_zone_mode(enabled and selected_pane == index)

def toggle_zoom(enabled):
    for index, panel in enumerate(video_panels):
        panel.set_zoom_mode(enabled and selected_pane == index)
    if enabled:
        select_button.setChecked(False)  # 대상 선택 끄기
        zone_button.setChecked(False)  # 구역 추가 끄기

select_button.toggled.connect(toggle_select)  # SELECT 버튼 연결
zone_button.toggled.connect(toggle_zone)
zoom_button.toggled.connect(toggle_zoom)
video_title = QLabel("관제 영상 · 선택된 영상 없음")  # 현재 영상 이름 표시
video_title.setStyleSheet("color: #E8F0FA; font-size: 16px; font-weight: bold;")  # 제목 모양
center_layout.addWidget(video_title)  # 영상 위에 제목 배치

split_menu = QComboBox()  # 화면 배치 선택 메뉴
split_menu.addItems(["1화면", "2분할", "4분할"])
center_layout.addWidget(split_menu)  # 영상 위에 메뉴 배치

video_container = QWidget()  # 영상 두 칸을 담는 공간
video_layout = QGridLayout(video_container)  # 4분할은 2행 2열 배치
video_layout.setContentsMargins(0, 0, 0, 0)
video_layout.setSpacing(8)
video_panels = [video_panel]
for index in range(1, 4):
    panel = VideoLabel(f"{index+1}번 영상\n\n클릭한 뒤 영상 라이브러리에서 선택해 주세요")
    panel.setAlignment(Qt.AlignmentFlag.AlignCenter)
    panel.setMinimumSize(1, 1)
    panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
    panel.setStyleSheet(video_panel.styleSheet())
    panel.set_zone_level(zone_level.currentText())
    zone_level.currentTextChanged.connect(panel.set_zone_level)
    video_panels.append(panel)

stats_label = StatsCards()  # 네 화면이 함께 사용하는 정보 카드
pane_stats = [(0, 0, 0.0) for _ in range(4)]
zone_states = [set() for _ in range(4)]
zone_ranks = [0 for _ in range(4)]
selected_pane = 0
focused_pane = False

def render_frame(panel, frame):
    if frame is None:
        return
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    panel.setPixmap(QPixmap.fromImage(image).scaled(panel.contentsRect().size(),
        Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

def render_zoom(frame, roi):
    if frame is not None and frame.size:
        zoom_panel.show_frame(frame, roi)  # 원본과 선택 범위를 함께 전달

def set_pane_stats(index, people, vehicles, fps):
    pane_stats[index] = (people, vehicles, fps)
    if selected_pane == index:
        stats_label.set_values(people, vehicles, fps)

class VideoPane(QObject):
    def __init__(self, index):
        super().__init__(window)
        self.index, self.panel = index, video_panels[index]
        self.cap, self.frame, self.roi = None, None, None
        self.objects, self.selected_id = [], None
        self.path, self.task_id = "", 0
        self.last_submit, self.at_end = 0.0, False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_video)
        self.worker = SecondAIWorker(self)
        self.worker.result_ready.connect(self.show_result)
        self.worker.error.connect(self.show_error)
        self.panel.region_selected.connect(self.select_zoom)
        self.panel.target_clicked.connect(self.select_target)

    def show_error(self, message):
        event_list.insertItem(0, f"{self.index+1}번: {message}")

    def stop(self):
        global zone_alert_rank
        self.timer.stop()
        self.task_id += 1  # 정지 전 분석 결과 무시
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.at_end = False
        zone_states[self.index].clear()
        zone_ranks[self.index] = 0
        zone_alert_rank = max(zone_ranks)
        update_safety_alert()
        if selected_pane == self.index:
            sync_selected_time()

    def load(self, path):
        new_cap = cv2.VideoCapture(path)
        if not new_cap.isOpened():
            new_cap.release()
            self.show_error("영상 파일을 열 수 없습니다.")
            return
        self.stop()
        self.cap, self.path = new_cap, path
        self.frame, self.roi, self.selected_id = None, None, None
        self.objects = []
        if selected_pane == self.index:
            video_title.setText(f"관제 영상 · {self.index+1}번 · {Path(path).name}")
        self.panel.zones.clear()
        self.panel.zone_points.clear()
        self.panel.selected_zone = None
        self.panel.moving_point = None
        self.last_submit = 0.0
        set_pane_stats(self.index, 0, 0, 0.0)
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.timer.setInterval(max(1, round(1000/(fps if 1 <= fps <= 120 else 30))))
        self.update_video()
        self.resume()

    def resume(self):
        if self.cap is None:
            if not self.path:
                open_selected_video()
                return
            self.load(self.path)
            return
        if self.at_end:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self.at_end = False
        self.timer.start()
        if selected_pane == self.index:
            status_label.setText(f"● {self.index+1}번 재생 중")
            sync_selected_time()

    def update_video(self):
        if self.cap is None:
            return
        ok, frame = self.cap.read()
        if not ok:
            self.timer.stop()
            self.at_end = True  # 영상 끝에서도 시간 이동 가능
            if selected_pane == self.index:
                status_label.setText(f"● {self.index+1}번 영상 끝 · 시간 이동 가능")
                sync_selected_time()
            return
        self.frame = frame
        now = monotonic()
        if now-self.last_submit >= 0.3:
            self.worker.submit(frame, self.task_id)
            self.last_submit = now
        self.render_current()
        if selected_pane == self.index:
            sync_selected_time()
            self.update_zoom()

    def show_result(self, people, vehicles, fps, task_id, objects, frame):
        if task_id != self.task_id or self.cap is None:
            return
        self.objects = objects
        self.render_current()
        set_pane_stats(self.index, people, vehicles, fps)
        check_zone_intrusion(objects, frame, self.index)
        if selected_pane == self.index:
            self.update_zoom()

    def render_current(self):
        if self.frame is None:
            return
        display = self.frame.copy()  # 확대용 원본에는 박스를 그리지 않음
        names = {"사람": "PERSON", "자동차": "CAR", "오토바이": "BIKE", "버스": "BUS", "트럭": "TRUCK"}
        for obj in self.objects:
            x1, y1, x2, y2 = map(int, obj["bbox"])
            color = (80, 220, 120) if obj["name"] == "사람" else (240, 180, 80)
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 1)
            cv2.putText(display, f"{names.get(obj['name'], 'OBJECT')} {obj['id']}",
                        (x1, max(14, y1-5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        render_frame(self.panel, display)

    def select_target(self, point):
        area = self.panel.image_rect()
        if area is None or self.frame is None:
            return
        h, w = self.frame.shape[:2]
        x, y = (point.x()-area.x())*w/area.width(), (point.y()-area.y())*h/area.height()
        matches = [obj for obj in self.objects if obj["bbox"][0] <= x <= obj["bbox"][2]
                   and obj["bbox"][1] <= y <= obj["bbox"][3]]
        if matches:
            target = min(matches, key=lambda obj: (obj["bbox"][2]-obj["bbox"][0])*(obj["bbox"][3]-obj["bbox"][1]))
            self.selected_id = target["id"]
            self.update_zoom()

    def select_zoom(self, rect):
        area = self.panel.image_rect()
        if area is None:
            return
        rect = rect.intersected(area)
        if rect.width() < 10 or rect.height() < 10:
            return
        self.selected_id = None
        self.roi = ((rect.left()-area.left())/area.width(), (rect.top()-area.top())/area.height(),
                    (rect.x()+rect.width()-area.left())/area.width(), (rect.y()+rect.height()-area.top())/area.height())
        zoom_name.hide()
        self.update_zoom()

    def update_zoom(self):
        if self.frame is None:
            return
        zoom_title.setText(f"확대 화면 · {self.index+1}번 영상")
        if self.selected_id is not None:
            target = next((obj for obj in self.objects if obj["id"] == self.selected_id), None)
            if target is not None:
                h, w = self.frame.shape[:2]
                x1, y1, x2, y2 = target["bbox"]
                desired = (max(0,x1-20)/w, max(0,y1-20)/h, min(w,x2+20)/w, min(h,y2+20)/h)
                self.roi = desired if self.roi is None else tuple(a*0.75+b*0.25 for a,b in zip(self.roi, desired))
                zoom_name.setText(f"{target['name']} · ID {target['id']}")
                zoom_name.adjustSize()
                zoom_name.show()
                zoom_name.raise_()
        if self.roi is not None:
            h, w = self.frame.shape[:2]
            x1, y1, x2, y2 = self.roi
            display_roi = (x1*w, y1*h, x2*w, y2*h)
            if self.selected_id is not None and target is not None:
                dx1, dy1, dx2, dy2 = desired
                display_roi = (min(x1,dx1)*w, min(y1,dy1)*h, max(x2,dx2)*w, max(y2,dy2)*h)
            render_zoom(self.frame, display_roi)

extra_panes = [VideoPane(index) for index in range(1, 4)]

def pane_timer(index):
    return timer if index == 0 else extra_panes[index-1].timer

def selected_capture():
    return cap if selected_pane == 0 else extra_panes[selected_pane-1].cap

def sync_selected_time():
    active_cap = selected_capture()
    total_frames = int(active_cap.get(cv2.CAP_PROP_FRAME_COUNT)) if active_cap is not None else 0
    video_slider.setEnabled(total_frames > 1)
    video_current.setEnabled(total_frames > 1)
    if video_slider.isSliderDown():
        return
    video_slider.setRange(0, max(0, total_frames-1))
    position = max(0, int(active_cap.get(cv2.CAP_PROP_POS_FRAMES))-1) if active_cap is not None else 0
    video_slider.setValue(position)
    fps = active_cap.get(cv2.CAP_PROP_FPS) if active_cap is not None else 0
    current, total = (int(position/fps), int(total_frames/fps)) if fps > 0 else (0, 0)
    if not video_current.hasFocus():
        video_current.setText(f"{current//60:02d}:{current%60:02d}")
    video_time.setText(f"/ {total//60:02d}:{total%60:02d}")

def refresh_panes():
    render_frame(video_panel, last_frame)
    for pane in extra_panes:
        pane.render_current()
    update_zoom()

def apply_view():
    while video_layout.count():
        video_layout.takeAt(0)  # 배치만 해제하고 영상과 구역은 유지
    visible = [selected_pane] if focused_pane else list(range((1, 2, 4)[split_menu.currentIndex()]))
    for index, panel in enumerate(video_panels):
        panel.setVisible(index in visible)
    for position, index in enumerate(visible):
        row, column = divmod(position, 2) if len(visible) == 4 else (0, position)
        video_layout.addWidget(video_panels[index], row, column)
    video_layout.setRowStretch(0, 1)
    video_layout.setRowStretch(1, 1 if len(visible) == 4 else 0)
    video_layout.setColumnStretch(0, 1)
    video_layout.setColumnStretch(1, 1 if len(visible) > 1 else 0)
    view_hint.setText(f"선택: {selected_pane+1}번 화면 · " +
        ("Esc로 분할 복귀" if focused_pane else "클릭: 조작 화면 선택 · 더블클릭: 크게 보기"))
    return_button.setVisible(focused_pane)
    QTimer.singleShot(0, refresh_panes)

def select_pane(index):
    global selected_pane
    if selected_pane != index:
        select_button.setChecked(False)
        zone_button.setChecked(False)
        zoom_button.setChecked(False)
        zoom_panel.clear()
        zoom_name.hide()
    selected_pane = index
    if index > 0:
        path = extra_panes[index-1].path
        video_title.setText(f"관제 영상 · {index+1}번 · " + (Path(path).name if path else "선택된 영상 없음"))
    else:
        video_title.setText("관제 영상 · 1번 화면")
    for i, panel in enumerate(video_panels):
        panel.setStyleSheet("background:#111F2E; color:#DCE6F2; font-size:18px; border-radius:10px; border:2px solid " +
                           ("#2EDDB5;" if i == index else "#263B50;"))
    stats_label.set_values(*pane_stats[index])
    sync_selected_time()
    status_label.setText(f"● {index+1}번 " + ("재생 중" if pane_timer(index).isActive() else "대기 / 일시정지"))
    apply_view()

def change_split(index):
    global focused_pane
    focused_pane = False
    if selected_pane >= (1, 2, 4)[index]:
        select_pane(0)
    else:
        apply_view()

def return_to_split():
    global focused_pane
    focused_pane = False
    apply_view()

view_hint = QLabel("선택: 1번 화면")
view_hint.setStyleSheet("color:#91A7BD; font-size:12px;")
return_button = QPushButton("분할 화면으로 복귀 · Esc")
return_button.clicked.connect(return_to_split)
return_button.hide()
center_layout.addWidget(view_hint)
center_layout.addWidget(return_button)
center_layout.addWidget(video_container, 1)
center_layout.addWidget(stats_label)
split_menu.currentIndexChanged.connect(change_split)

def open_selected_video():
    path, _ = QFileDialog.getOpenFileName(window, f"{selected_pane+1}번 영상 선택", "", "영상 파일 (*.mp4 *.mov *.avi *.mkv *.webm)")
    if not path:
        return
    if selected_pane > 0:
        extra_panes[selected_pane-1].load(path)
    else:
        item = QListWidgetItem(Path(path).name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        play_video(item)

open_button = QPushButton("＋ 선택한 화면에 영상 열기")
open_button.setStyleSheet(zoom_button.styleSheet())
open_button.clicked.connect(open_selected_video)
center_layout.addWidget(open_button)

def open_library_video(item):
    if selected_pane > 0:
        if item.data(Qt.ItemDataRole.UserRole+1):
            event_list.addItem("실시간 연결은 현재 1번 화면에서 지원합니다.")
            return
        extra_panes[selected_pane-1].load(item.data(Qt.ItemDataRole.UserRole))
    else:
        play_video(item)

class PaneEvents(QObject):
    def eventFilter(self, obj, event):
        global focused_pane
        if obj is window and event.type() == QEvent.Type.WindowStateChange:
            sync_fullscreen_button()  # macOS 기본 전체 화면 버튼으로 바꿔도 문구 동기화
            QTimer.singleShot(0, refresh_panes)
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if isinstance(obj, QWidget) and obj.window() is not window:
                return False  # 파일 선택 창의 Esc 동작 유지
            if video_current.hasFocus() or any(panel.zone_enabled and panel.zone_points for panel in video_panels):
                return False  # 시간 입력·작성 중인 구역 취소를 먼저 처리
            if window.isFullScreen():
                leave_fullscreen()
                return True
            if focused_pane:
                return_to_split()
                return True
        if obj in video_panels:
            index = video_panels.index(obj)
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                select_pane(index)
                obj.setFocus()
            elif event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                if not (obj.zoom_enabled or obj.select_enabled or obj.zone_enabled):
                    select_pane(index)
                    focused_pane = True
                    apply_view()
                    return True
            elif event.type() == QEvent.Type.Resize:
                QTimer.singleShot(0, refresh_panes)
        return False

pane_events = PaneEvents(window)
app.installEventFilter(pane_events)

video_slider = QSlider(Qt.Orientation.Horizontal)  # 가로 슬라이더
video_slider.setRange(0, 0)  # 영상을 열기 전에는 이동 범위 없음
video_slider.setEnabled(False)  # 영상 연결 전에는 비활성화
video_slider.setStyleSheet("""
    QSlider::groove:horizontal {
        height: 6px;
        background: #263B50;
        border-radius: 3px;
    }
    QSlider::handle:horizontal {
        width: 14px;
        margin: -4px 0;
        background: #2EDDB5;
        border-radius: 7px;
    }""")                                   # 막대와 손잡이 색상
center_layout.addWidget(video_slider)       # 영상 아래에 배치
time_layout = QHBoxLayout()  # 현재 시간과 전체 시간을 한 줄로 배치
time_layout.addStretch()  # 시간 표시를 오른쪽으로 이동
video_current = QLineEdit("00:00")  # 클릭해서 입력할 현재 시간
video_current.setFixedWidth(70)  # 입력칸 너비
video_current.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 가운데 정렬
video_current.setStyleSheet("color:#2EDDB5; background:#111F2E; border:1px solid #263B50; border-radius:4px; padding:4px;")
video_time = QLabel("/ 00:00")  # 전체 시간 표시
video_time.setStyleSheet("color:#91A7BD; font-size:12px;")
time_layout.addWidget(video_current)
time_layout.addWidget(video_time)
center_layout.addLayout(time_layout)
def seek_selected(position):
    if selected_pane > 0:
        extra_panes[selected_pane-1].at_end = False
    active_cap = selected_capture()
    if active_cap is None:
        return
    active_timer = pane_timer(selected_pane)
    was_playing = active_timer.isActive()
    active_timer.stop()
    active_cap.set(cv2.CAP_PROP_POS_FRAMES, position)
    if selected_pane > 0:
        extra_panes[selected_pane-1].update_video()
    else:
        update_frame()
    sync_selected_time()
    if was_playing and selected_capture() is active_cap:
        active_timer.start()  # 이동 전에 재생 중이었다면 계속 재생

def jump_to_time():
    active_cap = selected_capture()
    if active_cap is None:
        return
    try:
        minutes, seconds = map(int, video_current.text().strip().split(":"))
        if minutes < 0 or not 0 <= seconds < 60:
            raise ValueError
    except ValueError:
        video_current.setToolTip("02:30처럼 분:초로 입력해 주세요")
        video_current.selectAll()
        return
    fps = active_cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        return
    position = min(int((minutes * 60 + seconds) * fps), video_slider.maximum())
    video_current.clearFocus()
    seek_selected(position)

video_current.returnPressed.connect(jump_to_time)  # Enter로 시간 이동
def cancel_time_input():
    video_current.clearFocus()
    sync_selected_time()  # 선택한 영상의 실제 시간으로 복원

cancel_shortcut = QShortcut(QKeySequence("Escape"), video_current)  # 입력칸의 Esc
cancel_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)  # 입력칸에서만 작동
cancel_shortcut.activated.connect(cancel_time_input)  # Esc로 입력 취소

seek_was_playing = False
seek_pane = 0
def begin_seek():
    global seek_was_playing, seek_pane
    seek_pane = selected_pane  # 드래그를 시작한 화면 기억
    active_timer = pane_timer(seek_pane)
    seek_was_playing = active_timer.isActive()
    active_timer.stop()

def finish_seek():
    if selected_pane != seek_pane:
        if seek_was_playing:
            (pane_timer(seek_pane)).start()
        return
    seek_selected(video_slider.value())
    if seek_was_playing and selected_capture() is not None:
        (pane_timer(selected_pane)).start()
video_slider.sliderPressed.connect(begin_seek)  # 손잡이를 잡으면 실행
video_slider.sliderReleased.connect(finish_seek)  # 손잡이를 놓으면 실행

tools_layout = QHBoxLayout()                # 도구를 가로로 배치
tools_layout.setSpacing(8)                  # 도구 사이 간격
tools_layout.addWidget(select_button, 2)    # SELECT를 확대 버튼 왼쪽에 배치
tools_layout.addWidget(zoom_button, 2)      # 확대 영역 선택
tools_layout.addWidget(zone_level, 1)       # 주의·경고·위험 선택
tools_layout.addWidget(zone_button, 2)      # 구역 추가
center_layout.addLayout(tools_layout)       # 영상 바로 아래에 표시
playback_layout = QHBoxLayout()                   # 버튼을 가로로 배치
play_button = QPushButton("▶ 재생")                # 재생 버튼
pause_button = QPushButton("Ⅱ 일시정지")            # 일시정지 버튼
stop_button = QPushButton("■ 정지")                # 정지 버튼

for button in (play_button, pause_button, stop_button):  # 세 버튼에 같은 디자인 적용
    button.setMinimumHeight(42)  # 버튼 높이
    button.setStyleSheet("""
        QPushButton { background: #167D96; color: white; border: none;
                      border-radius: 6px; font-size: 14px; font-weight: bold; }
        QPushButton:hover { background: #209BB8; }
    """)
    playback_layout.addWidget(button)  # 버튼을 가로 줄에 추가
def resume_video():
    if selected_pane > 0:
        extra_panes[selected_pane-1].resume()
        return
    if stream_capture is not None:
        timer.start(33)  # 최신 실시간 영상 표시 다시 시작
        status_label.setText("● 실시간 재생 중")
    elif cap is not None:
        if cap.get(cv2.CAP_PROP_POS_FRAMES) >= cap.get(cv2.CAP_PROP_FRAME_COUNT):
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # 끝난 영상은 처음부터 재생
        timer.start()  # 일반 영상 이어서 재생
        status_label.setText("● 재생 중")
    else:
        item = video_list.currentItem()
        if item is not None:
            play_video(item)  # 선택한 일반 영상 재생

def pause_video():
    if selected_pane > 0:
        pane_timer(selected_pane).stop()
        status_label.setText(f"● {selected_pane+1}번 일시정지")
        return
    if (cap is not None or stream_capture is not None) and timer.isActive():
        timer.stop()  # 화면 갱신과 새 AI 분석 요청 멈추기
        status_label.setText("● 일시정지")

def reset_video():
    if selected_pane > 0:
        extra_panes[selected_pane-1].stop()
        set_pane_stats(selected_pane, 0, 0, 0.0)
        status_label.setText(f"● {selected_pane+1}번 정지")
        return
    global last_frame, zoom_roi  # 원본 프레임과 확대 영역 변경
    stop_video()  # 재생 중지와 영상 연결 해제
    set_pane_stats(0, 0, 0, 0.0)  # 정지 버튼을 누르면 수치 초기화
    video_slider.setRange(0, 0)  # 정지하면 슬라이더 초기화
    video_current.setText("00:00")  # 현재 시간 초기화
    video_time.setText("/ 00:00")  # 전체 시간 초기화
    last_frame = None  # 원본 프레임 초기화
    zoom_roi = None  # 확대 좌표 초기화
    zoom_button.setChecked(False)  # 확대 선택 모드 끄기
    zoom_panel.setText("확대할 영역을 선택해 주세요")  # 확대 화면 초기화
    video_panel.setText("관제 영상\n\n영상을 선택해 주세요")  # 메인 화면 초기화
    window.setWindowTitle("드론 안전 관제")  # 창 제목 초기화
    video_title.setText("관제 영상 · 선택된 영상 없음")  # 정지하면 제목 초기화

play_button.clicked.connect(resume_video)  # 재생 버튼과 함수 연결
pause_button.clicked.connect(pause_video)  # 일시정지 버튼과 함수 연결
stop_button.clicked.connect(reset_video)  # 정지 버튼과 함수 연결
center_layout.addLayout(playback_layout)  # 영상 아래에 버튼 줄 추가
layout.addWidget(center_panel, 6)  # 가운데 공간을 메인 화면에 추가
layout.addWidget(right_panel, 3)    # 오른쪽 너비 비중
window.setCentralWidget(container)  # 세 구역을 메인 창에 연결

stream_jobs = set()  # 실행 중인 연결 작업 보관

def stream_ready(task_id):
    if task_id != ai_task_id or stream_capture is None:
        return  # 이전 연결의 알림은 무시
    timer.start(33)  # 약 0.033초마다 최신 영상 확인
    status_label.setText("● 실시간 재생 중")
    stream_button.setEnabled(True)
    stream_button.setText("연결하기")


def stream_resolved(url, task_id):
    global stream_capture, last_frame, last_raw_frame, zoom_roi, selected_track_id

    if task_id != ai_task_id:
        return  # 이전 주소 추출 결과는 무시

    last_frame = None  # 이전 영상 초기화
    last_raw_frame = None
    zoom_roi = None  # 이전 확대 영역 초기화
    selected_track_id = None  # 이전 선택 대상 해제
    tracked_objects.clear()
    zoom_name.hide()
    zoom_title.setText("확대 화면")
    zoom_panel.setText("확대할 영역을 선택해 주세요")
    zoom_button.setChecked(False)

    video_slider.setRange(0, 0)  # 실시간 영상은 시간 이동 사용 안 함
    video_slider.setEnabled(False)
    video_current.setText("LIVE")
    video_current.setEnabled(False)  # 실시간 영상의 시간 입력 끄기
    video_time.setText("")
    video_title.setText("관제 영상 · 실시간 스트리밍")
    window.setWindowTitle("드론 안전 관제 · 실시간 스트리밍")
    video_panel.setText("실시간 영상을 연결하는 중입니다…")
    status_label.setText("● 영상 연결 중")
    stream_button.setText("영상 연결 중…")

    job = StreamCapture(url, task_id, window)  # 별도 작업에서 영상 읽기
    stream_capture = job                       # 현재 영상 작업 보관
    stream_capture_jobs.add(job)               # 종료 전까지 작업 유지
    job.ready.connect(stream_ready)            # 첫 영상 수신 알림 연결
    job.error.connect(stream_failed)           # 오류 처리 연결
    job.finished.connect(lambda: stream_capture_jobs.discard(job))
    job.finished.connect(job.deleteLater)      # 끝난 작업 객체 정리
    job.start()

def stream_failed(message, task_id):
    if task_id != ai_task_id:
        return  # 이전 연결의 오류는 무시
    stop_video()  # 오류가 난 영상 작업 정지
    status_label.setText("● 연결 실패")
    video_panel.setText(message)  # 오류 원인 표시

def resolve_stream():
    select_pane(0)
    url = stream_url.text().strip()
    if not url:
        stream_url.setFocus()
        return
    stop_video()  # 이전 영상 종료, 작업 번호 갱신
    stream_button.setEnabled(False)  # 추출 중에는 중복 클릭 방지
    stream_button.setText("주소 확인 중…")
    status_label.setText("● 연결 중")
    job = StreamResolver(url, ai_task_id, window)
    stream_jobs.add(job)  # 작업이 끝날 때까지 보관
    job.resolved.connect(stream_resolved)
    job.error.connect(stream_failed)
    job.finished.connect(lambda: stream_jobs.discard(job))
    job.start()  # 별도 스레드에서 주소 추출 시작

stream_button.clicked.disconnect(add_stream)  # 기존 목록 등록 동작 해제
stream_url.returnPressed.disconnect(add_stream)
stream_button.clicked.connect(resolve_stream)  # 버튼으로 주소 추출
stream_url.returnPressed.connect(resolve_stream)  # Enter로도 실행

for pane in extra_panes:
    pane.worker.start()  # 2·3·4번 영상의 분석 스레드 시작
ai_worker.start()                   # 별도 스레드에서 AI 모델 준비
select_pane(0)  # 첫 번째 화면을 기본 조작 대상으로 설정
window.show()                       # 완성한 창을 화면에 표시
sys.exit(app.exec())                # 클릭·키보드 입력을 기다리며 프로그램 유지
