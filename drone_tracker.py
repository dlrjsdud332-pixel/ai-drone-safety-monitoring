import subprocess  # 터미널 명령어 실행용
import cv2
import yt_dlp  # 유튜브 실제 영상 주소 가져오기
from ultralytics import YOLO
import time  # time 모듈을 불러옴 / 프레임 처리 시간을 측정해서 FPS를 계산할 때 사용
             #import = 파이썬에서 기능을 불러온다
             #time = 시간 측정 기능이 들어있는 파이썬 모듈

youtube_url = "https://www.youtube.com/watch?v=49CcWK-UqDQ"    # 유튜브 영상 주소
ydl_opts = {}                                                     # 별도 영상 형식을 강제로 지정하지 않고 yt-dlp가 자동 선택
with yt_dlp.YoutubeDL(ydl_opts) as ydl:                           # yt-dlp 실행 준비
    info = ydl.extract_info(youtube_url, download=False)          # 유튜브 영상 정보 가져오기
    stream_url = subprocess.check_output(["yt-dlp", "-g", youtube_url], text=True).splitlines()[0]  # 실제 영상 주소 가져오기
model = YOLO("models/yolo11s.pt")                                 # 가벼운 추적 모델                                        
ppe_model = YOLO("models/ppe_best.pt")                            # 안전모·안전조끼 전용 모델
pose_model = YOLO("models/yolo11n-pose.pt")                       # 사람의 몸과 관절 위치를 감지하는 경량 모델
ppe_results = None                                                # 가장 최근 PPE 감지 결과를 저장
frame_count = 0                                                   # 처리한 영상 프레임 수를 저장
was_upright = {}                                                  # 추적 ID별로 이전에 서 있었는지 저장
no_hardhat_until = 0                                              # 안전모 미착용 경고가 끝나는 시간
ppe_last_logged = {}                                              # (사람 ID, 미착용 장비)별 마지막 기록 시각
no_vest_until = 0                                                 # 안전조끼 미착용 경고가 끝나는 시간
pose_results = None                                               # 가장 최근 관절 감지 결과 저장
fall_until = {}                                                   # 추적 ID별 경고 종료 시간
danger_previous_ids = set()                                       # 이전 프레임에 위험구역 안에 있던 사람 ID\
danger_last_logged = {}                                           # ID별 마지막 위험구역 기록 시각
show_ppe = True                                                   # 거리 영상에서는 안전장비 경고를 표시하지 않음
track_history = {}                                                # track_history 딕셔너리에 각 추적 ID별 이동 좌표 기록을 저장 / {}=비어 있는 딕셔너리 생성 / 나중에 ID마다 좌표 목록을 따로 보관
last_seen = {}                                                    # 각 추적 ID가 마지막으로 감지된 시간을 저장
selected_id = None                                                # 현재 클릭해서 선택한 사람의 ID
click_boxes = {}                                                  # 클릭할 수 있는 사람 박스 위치 저장
drag_start = None                                                 # 드래그 시작 좌표
drag_end = None                                                   # 드래그 끝 좌표
drag_box = None                                                   # 확대할 영역
danger_start = None                                               # 위험구역 첫 번째 클릭 좌표
danger_end = None                                                 # 위험구역 두 번째 클릭 좌표
danger_box = None                                                 # 완성된 위험구역 좌표
is_setting_danger = False                                         # True: 위험구역을 지정하는 중
is_dragging = False                                               # 현재 드래그 중인지 확인
last_zoom = None                                                  # 마지막으로 확대된 화면 저장
last_zoom_time = 0                                                # 마지막으로 사람을 감지한 시간 저장
tool_mode = "SELECT"  
DISPLAY_WIDTH = 1440                                               # 실제로 보여줄 창 너비
DISPLAY_HEIGHT = 810                                              # 실제로 보여줄 창 높이                            
                # 현재 도구: 선택, 확대, 이동
def select_person(event, mouse_x, mouse_y, flags, param):
    global selected_id, drag_start, drag_end, drag_box, is_dragging, tool_mode
    global danger_start, danger_end, danger_box, is_setting_danger
    mouse_x = int(mouse_x * 1920 / DISPLAY_WIDTH)   # 창 좌표 → 원본 영상 좌표
    mouse_y = int(mouse_y * 1080 / DISPLAY_HEIGHT)
    if event == cv2.EVENT_LBUTTONDOWN:
            # 화면 위쪽 도구 버튼 클릭
        if mouse_y <= 50:

            if 10 <= mouse_x <= 110:
                tool_mode = "SELECT"

            elif 120 <= mouse_x <= 220:
                tool_mode = "ZOOM"
                is_dragging = False
                drag_start = None
                drag_end = None

            elif 230 <= mouse_x <= 330:
                tool_mode = "MOVE"

            elif 340 <= mouse_x <= 440:
                tool_mode = "SELECT"  # 초기화 후 다시 선택 모드
                drag_box = None
                selected_id = None
                danger_box = None          # 저장된 위험구역 삭제
                danger_start = None        # 위험구역 시작 좌표 초기화
                danger_end = None          # 위험구역 끝 좌표 초기화
                is_setting_danger = False  # 위험구역 설정 상태 종료

            elif 450 <= mouse_x <= 550:
                tool_mode = "DANGER"       # 위험구역 지정 모드
                is_setting_danger = False  # 지정 중 상태 초기화
                danger_start = None        # 첫 좌표 초기화
                danger_end = None          # 끝 좌표 초기화    
            return
        if tool_mode == "SELECT":
            selected_id = None

            for track_id, (x1, y1, x2, y2) in click_boxes.items():
                if x1 - 25 <= mouse_x <= x2 + 25 and y1 - 25 <= mouse_y <= y2 + 25:
                    selected_id = track_id
                    break

        elif tool_mode == "ZOOM":
            if not is_dragging:
                drag_start = (mouse_x, mouse_y)  # 첫 클릭: 시작점
                drag_end = drag_start
                is_dragging = True
            else:
                drag_end = (mouse_x, mouse_y)  # 두 번째 클릭: 끝점
                is_dragging = False

                x1, x2 = sorted((drag_start[0], drag_end[0]))
                y1, y2 = sorted((drag_start[1], drag_end[1]))

                if x2 - x1 > 20 and y2 - y1 > 20:
                    drag_box = (x1, y1, x2, y2)
        elif tool_mode == "MOVE" and drag_box is not None:
            x1, y1, x2, y2 = drag_box
            box_w = x2 - x1
            box_h = y2 - y1

            new_x1 = max(0, min(mouse_x - box_w // 2, 1920 - box_w))
            new_y1 = max(0, min(mouse_y - box_h // 2, 1080 - box_h))

            drag_box = (
                new_x1,
                new_y1,
                new_x1 + box_w,
                new_y1 + box_h
            )                                  # 클릭한 위치로 확대 영역 이동
        elif tool_mode == "DANGER":
            danger_start = (mouse_x, mouse_y)
            danger_end = danger_start
            is_setting_danger = True
    elif event == cv2.EVENT_LBUTTONUP:
        if tool_mode == "DANGER" and is_setting_danger and danger_start is not None:
            danger_end = (mouse_x, mouse_y)
            is_setting_danger = False
            x1, x2 = sorted((danger_start[0], danger_end[0]))
            y1, y2 = sorted((danger_start[1], danger_end[1]))
            if x2 - x1 > 20 and y2 - y1 > 20:
                danger_box = (x1, y1, x2, y2)
    elif event == cv2.EVENT_MOUSEMOVE:
        if is_dragging:
            drag_end = (mouse_x, mouse_y)  # ZOOM 영역이 마우스를 따라감
        elif is_setting_danger:
            danger_end = (mouse_x, mouse_y)  # 위험구역이 마우스를 따라감

    

cv2.namedWindow("Drone Tracking")
cv2.setMouseCallback("Drone Tracking", select_person)
cap = cv2.VideoCapture(stream_url)                            # 유튜브 실시간 영상 연결
#cap = cv2.VideoCapture("http://192.168.219.134:8080/video")
                         #이 부분은 카메라 주소 입력

while True:
    start_time = time.time()                                               # start_time 변수에 현재 시간을 대입 / time.time()=현재 시각을 초 단위 숫자로 가져오는 함수 / 프레임 처리가 시작된 시간을 기록
    ret, frame = cap.read()
    if not ret:
        if cap.get(cv2.CAP_PROP_FRAME_COUNT) > 0:
            fall_until.clear()
            was_upright.clear()
        print("영상 신호 끊김 - 다시 연결 시도")                         # 프레임을 못 받으면 재연결 시도
        cap.release()
        cap = cv2.VideoCapture(stream_url)
        continue
    frame_count += 1  # 영상 한 장을 읽을 때마다 숫자 1 증가
    results = model.track(frame, device="mps", persist=True, tracker="bytetrack.yaml", conf=0.10, iou=0.5, classes=[0, 1, 2, 3, 5, 7, 14, 15, 16], imgsz=704, verbose=False)  # ByteTrack으로 추적 ID 생성
    # results 변수에 추적 결과를 대입 / frame=현재 영상 프레임 / persist=True=이전 프레임의 추적 ID 유지 / conf=0.10=신뢰도 15% 이상 사용 / iou=0.5=중복 박스 억제 기준 / classes=[0, 2]=사람(person)과 자동차(car)만 탐지 / imgsz=1920=YOLO가 분석할 입력 이미지 크기를 크게 해서 멀리 있는 작은 사람의 특징을 더 잘 보게 함
    boxes = results[0].boxes                                              #boxes 변수에 탐지된 박스 목록을 대입 / results[0]=현재 프레임의 탐지 결과 / .boxes=탐지된 객체들의 박스 정보
    
    if frame_count % 10 == 0:  # 10프레임마다 한 번만 PPE 검사
        ppe_results = ppe_model.predict(
            frame,                    # 현재 영상 화면 한 장
            conf=0.25,                # 신뢰도 25% 이상만 사용
            imgsz=640,                # 검사할 영상 크기
            classes=[0, 1, 2, 4],     # 안전모·미착용·조끼만 검사
            device="mps",             # 맥북 M4 그래픽 가속
            verbose=False             # 터미널 반복 출력 숨김
        )[0] if show_ppe else None
        pose_results = pose_model.predict(
            frame,                    # 현재 영상 한 장
            conf=0.35,                # 신뢰도 35% 이상인 사람만 사용
            imgsz=640,                # 관절을 검사할 영상 크기
            device="mps",             # 맥북 M4 그래픽 가속 사용
            verbose=False             # 터미널 반복 출력 숨김
        )[0]
        for pose_box in pose_results.boxes.xyxy:
            x1, y1, x2, y2 = map(float, pose_box.tolist())
            best_id = None
            best_overlap = 0

            for box in boxes:
                if box.id is None or int(box.cls[0]) != 0:
                    continue

                bx1, by1, bx2, by2 = map(float, box.xyxy[0].tolist())
                width = max(0, min(x2, bx2) - max(x1, bx1))
                height = max(0, min(y2, by2) - max(y1, by1))
                intersection = width * height
                area_pose = (x2 - x1) * (y2 - y1)
                area_track = (bx2 - bx1) * (by2 - by1)
                overlap = intersection / (area_pose + area_track - intersection + 1e-6)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_id = int(box.id[0])

            if best_overlap < 0.3:
                continue
            if (x2 - x1) > (y2 - y1) * 1.8:
                if was_upright.get(best_id, False):
                    print(f"넘어짐 의심 ID: {best_id} | 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    with open("data/fall_events.csv", "a", encoding="utf-8") as log_file:
                        if log_file.tell() == 0:
                            log_file.write("time,track_id\n")
                        log_file.write(
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')},{best_id}\n"
                        )

                    fall_until[best_id] = time.time() + 10  # 지금부터 10초
                    was_upright[best_id] = False
            elif (y2 - y1) > (x2 - x1) * 1.2:
                was_upright[best_id] = True

    annotated_frame = results[0].plot(labels=False, conf=False, line_width=1)  # YOLO 기본 글자와 신뢰도 숨김 #
    if pose_results is not None:
        annotated_frame = pose_results.plot(
            img=annotated_frame,  # 기존 객체 추적 화면 위에 표시
            labels=False,         # 사람 이름 숨김
            conf=False,           # 신뢰도 숫자 숨김
            boxes=False,          # 포즈 모델의 사람 박스 숨김
            kpt_radius=3,         # 관절점 크기
            kpt_line=True         # True: 관절 사이 연결선 표시
    )
    if show_ppe and ppe_results is not None:
        annotated_frame = ppe_results.plot(
            img=annotated_frame,  # 기존 사람·자동차 화면 위에 PPE 박스 추가
            labels=True,          # True: Hardhat, Safety Vest 이름 표시
            conf=True,            # True: 감지 신뢰도 표시
            line_width=2          # 박스 선 굵기
        )
        ppe_classes = [
            int(ppe_box.cls[0])  # 감지된 종류 번호
            for ppe_box in ppe_results.boxes
            if float(ppe_box.conf[0]) >= 0.50  # 신뢰도 50% 이상만 경고에 사용
        ]
        current_time = time.time()  # 현재 시간

            # 미착용이 감지되면 경고 종료 시간을 10초 뒤로 갱신
        if 1 in ppe_classes:
            no_hardhat_until = current_time + 10
            
        if 2 in ppe_classes:
            no_vest_until = current_time + 10

        # 마지막 감지 후 10초 동안 경고문 표시
        if current_time < no_hardhat_until:
            cv2.putText(annotated_frame,"WARNING: NO HARDHAT",(1200, 100),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0, 0, 255),3)

        if current_time < no_vest_until:
            cv2.putText(annotated_frame,"WARNING: NO SAFETY VEST",(1200, 160),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0, 0, 255),3)
    person_count = 0
    car_count = 0                                                              # 현재 화면의 자동차 수
    click_boxes.clear()                                                        # 이전 화면의 박스 위치를 지우고 현재 화면 기준으로 다시 저장
    danger_current_ids = set()  # 이번 프레임에 위험구역 안에 있는 사람 ID
    for box in boxes:
       class_id = int(box.cls[0])
       track_id = int(box.id[0]) if box.id is not None else -1
       if track_id != -1:                                                      #if = 만약 / #track_id != -1 = 추적 ID가 -1이 아니라면 / #: = 그러면 아래 코드를 실행   
           last_seen[track_id] = time.time()                                   # 이 ID를 마지막으로 본 시간을 갱신 
           x, y, w, h = box.xywh[0]
           box_x1, box_y1, box_x2, box_y2 = map(int, box.xyxy[0].tolist())
           click_boxes[track_id] = (box_x1, box_y1, box_x2, box_y2)
           x1 = max(5, int(x - w / 2) - 25)                                         # 왼쪽 화면 밖으로 안 나가게
           y1 = max(20, int(y - h / 2))                                        # 위쪽 화면 밖으로 안 나가게
           center = (int(x), int(y))
           if track_id not in track_history:
               track_history[track_id] = []
           track_history[track_id].append(center)
           points = track_history[track_id]
           if len(points) > 60:                                 #~만약 길이    
               points.pop(0)                                    #.pop() → 목록에서 하나를 꺼내면서 삭제
               for i in range(1, len(points)):
                    pt1 = points[i - 1]                         # 이전 좌표
                    pt2 = points[i]                             # 현재 좌표
                    distance = ((pt2[0] - pt1[0]) ** 2 + (pt2[1] - pt1[1]) ** 2) ** 0.5

                    if distance < 80:                           # 이전 위치와 현재 위치의 거리가 80픽셀 미만일 때만 궤적 연결
                        cv2.line(annotated_frame, pt1, pt2, (255, 0, 255), 5)  # 노란 궤적 선 두께를 5로 표시

               if len(points) >= 5:                             # 좌표가 5개 이상 쌓였을 때 방향 표시
                    start = points[-5]                          # 5개 전 위치를 사용해 이동 방향을 더 크게 표시
                    end = points[-1]                            # 현재 위치
                    dx = end[0] - start[0]                      # 가로 이동량 계산: +면 오른쪽, -면 왼쪽
                    dy = end[1] - start[1]                      # 세로 이동량 계산: +면 아래쪽, -면 위쪽
                    speed = (dx ** 2 + dy ** 2) ** 0.5
                    if speed < 3:                               # 화면상 이동량이 작으면 정지 상태로 판단
                        direction = "STOP"
                    elif abs(dx) > abs(dy):                        # 가로 이동이 세로 이동보다 크면 좌우 방향 판단
                        direction = "RIGHT" if dx > 0 else "LEFT"  # dx가 +면 오른쪽, -면 왼쪽
                    else:                                          # 가로보다 세로 이동이 더 크면 위/아래 방향 판단
                        direction = "DOWN" if dy > 0 else "UP"     # dy가 +면 아래쪽, -면 위쪽
                    if direction != "STOP":  # 정지 상태가 아닐 때만 화살표 표시    
                        cv2.arrowedLine(annotated_frame, start, end, (0, 0, 255), 3, tipLength=0.4)  # 최신 이동 방향만 표시
                    
                    if class_id == 0:
                        object_name = "HUMAN"
                        box_color = (0, 255, 0)       # 초록
                    elif class_id in (2, 3, 5, 7):
                        object_name = "VEHICLE"
                        box_color = (255, 0, 0)       # 파랑
                    else:
                        object_name = "OBJECT"
                        box_color = (0, 255, 255)     # 노랑
                    label = f"{object_name} ID:{track_id} | {direction} | Speed:{speed:.1f}"  # ID, 방향, 속도를 한 줄로 정리
                    (text_w, text_h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
                    x1 = min(x1, annotated_frame.shape[1] - text_w - 10)                 # 오른쪽 화면 밖으로 안 나가게
                    cv2.rectangle(annotated_frame, (x1, y1 - text_h - 10), (x1 + text_w + 6, y1), (0, 0, 0), -1)  # 라벨 뒤 검은 배경
                    cv2.putText(annotated_frame, label, (x1, y1 - 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 3)         # 검은 외곽선

                    cv2.putText(annotated_frame, label, (x1, y1 - 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)   # 흰 글씨
                    if class_id == 0:
                            person_count += 1
                            if show_ppe and frame_count % 10 == 0 and ppe_results is not None and track_id != -1:
                                missing_items = set()  # 이번 검사에서 이 사람에게 감지된 미착용 항목
                                for ppe_box in ppe_results.boxes:
                                    item = int(ppe_box.cls[0])  # 1: 안전모 없음, 2: 조끼 없음
                                    if item not in (1, 2) or float(ppe_box.conf[0]) < 0.50:
                                        continue
                                    px1, py1, px2, py2 = map(float, ppe_box.xyxy[0].tolist())
                                    center_x = (px1 + px2) / 2
                                    center_y = (py1 + py2) / 2
                                    if box_x1 <= center_x <= box_x2 and box_y1 <= center_y <= box_y2:
                                        missing_items.add(item)
                                now = time.time()
                                for item in missing_items:
                                    key = (track_id, item)
                                    if now - ppe_last_logged.get(key, 0) >= 10:
                                        with open("data/ppe_events.csv", "a", encoding="utf-8") as log_file:
                                            if log_file.tell() == 0:
                                                log_file.write("time,track_id,missing_item\n")
                                            name = "NO_HARDHAT" if item == 1 else "NO_SAFETY_VEST"
                                            log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{track_id},{name}\n")
                                        ppe_last_logged[key] = now
                            cv2.rectangle(
                                annotated_frame,
                                (box_x1, box_y1),       # 사람 박스 왼쪽 위
                                (box_x2, box_y2),       # 사람 박스 오른쪽 아래
                                box_color, 2)           # 초록색, 선 굵기 2
                            if danger_box is not None:
                                dx1, dy1, dx2, dy2 = danger_box

                                foot_x = (box_x1 + box_x2) // 2  # 사람 발의 가로 중심
                                foot_y = box_y2                  # 사람 박스의 맨 아래
                                if dx1 <= foot_x <= dx2 and dy1 <= foot_y <= dy2:
                                    if track_id != -1:
                                        danger_current_ids.add(track_id)
                                    cv2.rectangle(annotated_frame,
                                    (box_x1, box_y1),
                                    (box_x2, box_y2),
                                    (0, 0, 255), 4)
                                    cv2.putText(
                                        annotated_frame,
                                        "DANGER! INTRUSION",
                                        (950, 100),  # 숫자가 커질수록 오른쪽으로 이동
                                        cv2.FONT_HERSHEY_SIMPLEX,
                                        1.2, (0, 0, 255), 3)
                    elif class_id in (2, 3, 5, 7):
                        car_count += 1  # 자동차·오토바이·버스·트럭
                    if class_id != 0:
                        cv2.rectangle(
                            annotated_frame,
                            (box_x1, box_y1),
                            (box_x2, box_y2),
                            box_color, 2
                        )
    new_danger_ids = danger_current_ids - danger_previous_ids

    for tid in new_danger_ids:
        now = time.time()
        if now - danger_last_logged.get(tid, 0) >= 10:
            with open("data/danger_events.csv", "a", encoding="utf-8") as log_file:
                if log_file.tell() == 0:
                    log_file.write("time,track_id\n")
                log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{tid}\n")
            danger_last_logged[tid] = now

    danger_previous_ids = danger_current_ids
    old_ids = [tid for tid, t in last_seen.items() if time.time() - t > 10]
    for tid in old_ids:
        track_history.pop(tid, None)
        last_seen.pop(tid, None)
        print(f"삭제 ID: {tid}")  # 10초 이상 안 보인 ID가 실제로 삭제됐는지 확인

    end_time = time.time()                    # end_time 변수에 현재 시간을 대입 / time.time()=현재 시각을 초 단위로 가져옴 / 한 프레임의 처리가 끝난 시간을 기록
    fps = 1 / (end_time - start_time)         # fps 변수에 초당 처리 가능한 프레임 수를 대입 / end_time-start_time=프레임 1장을 처리하는 데 걸린 시간(초) / 1을 처리시간으로 나누어 FPS 계산
    cv2.putText(annotated_frame, f"People: {person_count}", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(annotated_frame, f"FPS: {fps:.1f}", (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(annotated_frame, f"Vehicles: {car_count}", (20, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    if selected_id in click_boxes:
        tx1, ty1, tx2, ty2 = click_boxes[selected_id]
        last_target_center = ((tx1 + tx2) // 2, (ty1 + ty2) // 2)
    if selected_id in click_boxes:                                # 이 줄을 새로 추가
        sx1, sy1, sx2, sy2 = click_boxes[selected_id]
        cv2.rectangle(annotated_frame, (sx1, sy1), (sx2, sy2), (0, 255, 255), 4)
        pad = 60
        frame_h, frame_w = frame.shape[:2]
        crop_x1 = max(0, sx1 - pad)
        crop_y1 = max(0, sy1 - pad)
        crop_x2 = min(frame_w, sx2 + pad)
        crop_y2 = min(frame_h, sy2 + pad)
        zoom = frame[crop_y1:crop_y2, crop_x1:crop_x2]

        if zoom.size > 0:
            zoom = cv2.resize(zoom, (320, 240), interpolation=cv2.INTER_CUBIC)
            blurred = cv2.GaussianBlur(zoom, (0, 0), 1.0)
            zoom = cv2.addWeighted(zoom, 1.5, blurred, -0.5, 0)
            cv2.rectangle(zoom, (0, 0), (319, 239), (0, 255, 255), 3)
            cv2.putText(zoom, f"Selected ID: {selected_id}", (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            last_zoom = zoom.copy()
            last_zoom_time = time.time()
            annotated_frame[frame_h - 250:frame_h - 10,
                            frame_w - 330:frame_w - 10] = zoom
            
    elif selected_id is not None and last_zoom is not None:
            if time.time() - last_zoom_time < 5:
                frame_h, frame_w = annotated_frame.shape[:2]
                lost_zoom = last_zoom.copy()

                cv2.putText(lost_zoom, "TRACKING LOST", (10, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

                annotated_frame[frame_h - 250:frame_h - 10,
                        frame_w - 330:frame_w - 10] = lost_zoom
            else:
                selected_id = None  # 화면 위쪽 도구 버튼 표시
                last_zoom = None    # 드래그 중인 영역을 하늘색 사각형으로 표시
    # 지정 중인 위험구역을 빨간 사각형으로 표시
    if is_setting_danger and danger_start is not None and danger_end is not None:
        cv2.rectangle(
            annotated_frame, danger_start, danger_end, (0, 0, 255), 3)

    # 완성된 위험구역을 계속 표시
    if danger_box is not None:
        dx1, dy1, dx2, dy2 = danger_box
        cv2.rectangle(
            annotated_frame, (dx1, dy1), (dx2, dy2), (0, 0, 255), 3)
    if is_dragging and drag_start is not None and drag_end is not None:
        cv2.rectangle(annotated_frame, drag_start, drag_end,
                      (255, 255, 0), 3)

    # 드래그가 끝난 영역을 새 창으로 확대
    if drag_box is not None:
        dx1, dy1, dx2, dy2 = drag_box
        frame_h, frame_w = frame.shape[:2]

        dx1 = max(0, min(dx1, frame_w - 1))
        dy1 = max(0, min(dy1, frame_h - 1))
        dx2 = max(dx1 + 1, min(dx2, frame_w))
        dy2 = max(dy1 + 1, min(dy2, frame_h))

        area_zoom = frame[dy1:dy2, dx1:dx2]
        if area_zoom.size > 0:
            area_zoom = cv2.resize(
                area_zoom, (640, 360),
                interpolation=cv2.INTER_LANCZOS4  # 확대 화질 개선
            )
            blurred = cv2.GaussianBlur(area_zoom, (0, 0), 1.0)
            area_zoom = cv2.addWeighted(area_zoom, 1.5, blurred, -0.5, 0)  # 확대 화면 선명화

            cv2.imshow("Area Zoom", area_zoom)
            cv2.moveWindow("Area Zoom", 980, 50)  # 확대 창을 오른쪽으로 이동
    buttons = [
        ("SELECT", 10, 110),
        ("ZOOM", 120, 220),
        ("MOVE", 230, 330),
        ("RESET", 340, 440),
        ("DANGER", 450, 550)
    ]
    for button_name, button_x1, button_x2 in buttons:
        button_color = (0, 160, 0) if tool_mode == button_name else (60, 60, 60)

        if button_name == "RESET":
            button_color = (0, 0, 180)

        cv2.rectangle(annotated_frame, (button_x1, 5),
                      (button_x2, 45), button_color, -1)
        cv2.putText(annotated_frame, button_name,
                    (button_x1 + 12, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            
        now = time.time()
        fall_until = {fall_id: until for fall_id, until in fall_until.items() if now < until}

        for row, fall_id in enumerate(list(fall_until)[:3]):
            cv2.putText(
                annotated_frame, f"WARNING: POSSIBLE FALL ID:{fall_id}",
                (30, 250 + row * 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2
            )

    display_frame = cv2.resize(annotated_frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
    cv2.imshow("Drone Tracking", display_frame)
    key = cv2.waitKey(1) & 0xFF
    if key == ord("q") or key == 27:  # q 또는 ESC
        break
cap.release()
cv2.destroyAllWindows()
