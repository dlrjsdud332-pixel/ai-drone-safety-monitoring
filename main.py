import sys
import cv2                                      # 영상 프레임 읽기
from PySide6.QtCore import QPoint, QRect, Signal  # 좌표와 사각형, 선택 완료 신호
from PySide6.QtWidgets import QRubberBand  # 드래그 선택 사각형
from PySide6.QtCore import QTimer          # 일정 간격으로 화면 갱신
from PySide6.QtGui import QImage, QPixmap  # OpenCV 영상을 UI 이미지로 변환
from PySide6.QtWidgets import QSizePolicy  # 영상 영역의 크기 조절
from PySide6.QtGui import QPolygonF        # 여러 점으로 만든 다각형
from pathlib import Path                   # 파일 경로와 이름 처리
from PySide6.QtWidgets import QFileDialog, QListWidgetItem  # 파일 선택 창과 목록 항목
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QWidget, QHBoxLayout
from PySide6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QListWidget
from PySide6.QtGui import QPainter, QPen, QColor  # 화면에 점·선·색을 그리는 도구
from PySide6.QtCore import QPointF         # 소수점 좌표를 저장하는 도구
from PySide6.QtWidgets import QComboBox    # 여러 항목 중 하나를 선택하는 메뉴
from PySide6.QtCore import Qt

class VideoLabel(QLabel):
    region_selected = Signal(QRect)

    def __init__(self, text):
        super().__init__(text)
        self.zoom_enabled = False
        self.zone_enabled = False
        self.zone_points = []                           # 작성 중인 구역의 점
        self.zones = []                                 # 완성한 구역 목록
        self.zone_level = "위험"                         # 새 구역에 적용할 등급
        self.moving_point = None                        # 이동 중인 구역과 점 번호
        self.drag_start = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)

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
        if self.zone_enabled and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
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

        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.image_rect()
        if rect is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = {"주의": "#FFD54F", "경고": "#FF9800", "위험": "#FF5252"}

        def draw_zone(saved_points, level, closed):
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

            painter.setPen(QPen(color, 1))
            for i in range(1, len(points)):
                painter.drawLine(points[i - 1], points[i])
            if closed and len(points) >= 3:
                painter.drawLine(points[-1], points[0])

            painter.setBrush(color)
            for point in points:
                painter.drawEllipse(point, 3, 3)

        for zone in self.zones:
            draw_zone(zone["points"], zone["level"], True)  # 완성한 구역 표시

        draw_zone(self.zone_points, self.zone_level, False) # 작성 중인 구역 표시
        painter.end()

    def mousePressEvent(self, event):
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
                        return                          # 가까운 점을 잡아서 이동

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

window.setStyleSheet("""QMainWindow {background-color: #0B1420;}QLabel {color: #E8F0FA;font-size: 28px;font-weight: bold;}""")  # 배경색과 글자 모양 설정

container = QWidget()  # 세 구역을 담을 바탕
layout = QHBoxLayout(container)  # 구역을 가로로 배치
layout.setContentsMargins(16, 16, 16, 16)  # 왼쪽·위·오른쪽·아래 여백
layout.setSpacing(12)  # 구역 사이 간격

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

library_title = QLabel("영상 라이브러리")  # 상단 제목
add_button = QPushButton("+ 영상 추가")  # 영상 선택 버튼
video_list = QListWidget()  # 추가한 영상 이름을 표시할 목록

left_layout.addWidget(library_title)  # 제목 배치
left_layout.addWidget(add_button)  # 버튼 배치
left_layout.addWidget(video_list, 1)  # 남은 높이를 목록으로 채우기

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
zoom_panel = QLabel("확대할 영역을 선택해 주세요")  # 확대 영상을 표시할 자리
zoom_panel.setAlignment(Qt.AlignmentFlag.AlignCenter)
zoom_panel.setMinimumSize(1, 1)
zoom_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
zoom_panel.setStyleSheet("background: #0B1420; border-radius: 6px; color: #91A7BD; font-size: 14px;")

event_title = QLabel("이벤트 기록")  # 경고 목록 제목
event_list = QListWidget()  # 감지된 경고를 표시할 목록

right_layout.addWidget(zoom_title)
right_layout.addWidget(zoom_panel, 1)  # 위쪽 확대 화면
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
cap = None  # 현재 열려 있는 영상
last_frame = None  # 현재 화면의 원본 영상 보관
zoom_roi = None  # 확대할 영역의 원본 좌표 보관
timer = QTimer(window)  # 영상 화면을 갱신할 타이머

def stop_video():  # 재생 중지와 영상 파일 연결 해제
    global cap  # 함수 밖의 cap 사용
    timer.stop()
    if cap is not None:
        cap.release()
        cap = None
def update_zoom():  # 선택한 영역을 오른쪽 확대 화면에 표시
    if last_frame is None or zoom_roi is None:
        return  # 영상이나 선택 영역이 없으면 종료
    x1, y1, x2, y2 = zoom_roi  # 확대 영역의 왼쪽 위와 오른쪽 아래 좌표
    crop = last_frame[y1:y2, x1:x2]  # 원본에서 선택한 부분만 자르기
    if crop.size == 0:
        return  # 잘라낸 영상이 비어 있으면 종료
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)  # Qt 화면에 맞게 색상 순서 변경
    height, width = rgb.shape[:2]  # 잘라낸 영상의 세로와 가로
    image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()  # Qt 이미지 생성
    pixmap = QPixmap.fromImage(image)  # 잘라낸 영상을 화면용 이미지로 변환
    size = zoom_panel.contentsRect().size()  # 오른쪽 확대 화면 크기
    scaled = pixmap.scaled(
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,  # 비율을 유지하며 화면을 꽉 채우기
        Qt.TransformationMode.SmoothTransformation  # 부드럽게 확대
    )
    x = (scaled.width() - size.width()) // 2  # 가로에서 넘치는 부분의 절반
    y = (scaled.height() - size.height()) // 2  # 세로에서 넘치는 부분의 절반
    zoom_panel.setPixmap(scaled.copy(x, y, size.width(), size.height()))  # 중앙 부분만 표시

def select_zoom(rect):  # 화면에서 선택한 영역을 원본 영상 좌표로 변환
    global zoom_roi  # 함수 밖의 확대 영역 변경
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

def update_frame():  # 영상 한 장을 읽어 가운데에 표시
    global last_frame  # 함수 밖의 현재 영상 변수 사용
    if cap is None:
        return
    ok, frame = cap.read()  # ok: 읽기 성공 여부, frame: 영상 한 장
    if not ok:  # 영상이 끝났거나 읽지 못하면 중지
        stop_video()
        return
    last_frame = frame.copy()  # 확대에 사용할 원본 프레임 보관
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # OpenCV 색상을 UI 색상 순서로 변경
    height, width = rgb.shape[:2]  # 영상의 세로와 가로
    image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    pixmap = QPixmap.fromImage(image)  # 화면에 표시할 이미지 생성
    video_panel.setPixmap(pixmap.scaled(video_panel.contentsRect().size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))  # 비율 유지
    update_zoom()  # 메인 영상과 함께 확대 화면 갱신
def play_video(item):
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
    window.setWindowTitle(f"드론 안전 관제 · {item.text()}")  # 재생 중인 파일 이름 표시
    update_frame()  # 첫 화면 바로 표시
    if cap is not None:
        timer.start(max(1, round(1000 / fps)))    # FPS에 맞춰 반복 갱신

timer.timeout.connect(update_frame)               # 타이머가 울리면 다음 프레임 표시
video_list.itemDoubleClicked.connect(play_video)  # 목록 더블클릭과 재생 연결
app.aboutToQuit.connect(stop_video)               # 프로그램 종료 시 영상 연결 해제
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
center_layout.addWidget(zoom_button)              # 영상 위에 버튼 배치
zone_level = QComboBox()                          # 선택한 위험 등급을 담는 메뉴
zone_level.addItems(["주의", "경고", "위험"])        # 메뉴에 표시할 세 가지 등급
zone_level.setCurrentText("위험")                  # 처음에는 위험 등급을 선택
zone_level.currentTextChanged.connect(video_panel.set_zone_level)  # 선택 메뉴와 구역 색 연결
zone_level.setMinimumHeight(40)                   # 메뉴의 최소 높이
zone_level.setStyleSheet("background: #182C40; color: #DCE6F2; border: 1px solid #263B50; border-radius: 6px; padding: 6px;")
center_layout.addWidget(zone_level)               # 확대 버튼 아래에 메뉴 배치
zone_button = QPushButton("＋ 구역 추가")            # 드래그로 구역을 만들 때 사용할 버튼
zone_button.setCheckable(True)                    # 누르면 켜지고, 다시 누르면 꺼짐
zone_button.setMinimumHeight(40)                  # 버튼의 최소 높이
zone_button.setStyleSheet(zoom_button.styleSheet())  # 확대 버튼과 같은 디자인
center_layout.addWidget(zone_button)  # 등급 메뉴 아래에 버튼 배치

def toggle_zone(enabled):
    if enabled:
        zoom_button.setChecked(False)  # 확대 모드를 먼저 끔
    video_panel.set_zone_mode(enabled)  # 구역 그리기 모드에 버튼 상태 전달

def toggle_zoom(enabled):  # enabled: 확대 버튼이 켜졌는지 여부
    if enabled:
        zone_button.setChecked(False)  # 구역 추가 모드를 끔

zone_button.toggled.connect(toggle_zone)  # 구역 버튼 상태가 바뀌면 실행
zoom_button.toggled.connect(toggle_zoom)  # 확대 버튼 상태가 바뀌면 실행
center_layout.addWidget(video_panel, 1)           # 영상이 남는 공간을 채움
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
def resume_video():  # 일시정지한 영상 이어서 재생
    if cap is not None:
        timer.start()  # 기존 프레임 간격으로 다시 재생
    else:
        item = video_list.currentItem()  # 목록에서 선택한 영상
        if item is not None:
            play_video(item)  # 열린 영상이 없으면 처음부터 재생

def pause_video():  # 현재 화면에서 일시정지
    timer.stop()  # 화면 갱신만 멈추고 영상 연결은 유지

def reset_video():
    global last_frame, zoom_roi  # 원본 프레임과 확대 영역 변경
    stop_video()  # 재생 중지와 영상 연결 해제
    last_frame = None  # 원본 프레임 초기화
    zoom_roi = None  # 확대 좌표 초기화
    zoom_button.setChecked(False)  # 확대 선택 모드 끄기
    zoom_panel.setText("확대할 영역을 선택해 주세요")  # 확대 화면 초기화
    video_panel.setText("관제 영상\n\n영상을 선택해 주세요")  # 메인 화면 초기화
    window.setWindowTitle("드론 안전 관제")  # 창 제목 초기화

play_button.clicked.connect(resume_video)  # 재생 버튼과 함수 연결
pause_button.clicked.connect(pause_video)  # 일시정지 버튼과 함수 연결
stop_button.clicked.connect(reset_video)  # 정지 버튼과 함수 연결
center_layout.addLayout(playback_layout)  # 영상 아래에 버튼 줄 추가
layout.addWidget(center_panel, 6)  # 가운데 공간을 메인 화면에 추가
layout.addWidget(right_panel, 3)    # 오른쪽 너비 비중
window.setCentralWidget(container)  # 세 구역을 메인 창에 연결
window.show()                       # 완성한 창을 화면에 표시
sys.exit(app.exec())                # 클릭·키보드 입력을 기다리며 프로그램 유지