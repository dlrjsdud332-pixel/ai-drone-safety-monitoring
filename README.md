# AI 기반 실시간 드론 안전 관제 시스템

산업 현장과 공공장소의 안전사고를 빠르게 발견하기 위해 개발한 실시간 Vision AI 관제 프로젝트입니다. YOLO와 OpenCV를 활용하여 영상 속 사람·차량·사물을 탐지하고, ByteTrack 기반 추적 ID를 부여해 객체의 위치·이동 방향·속도·이동 경로를 지속적으로 분석합니다.


관제자는 화면에서 특정 대상을 선택해 확대하거나, 감시할 위험구역을 직접 지정할 수 있습니다. 시스템은 작업자의 안전모·안전조끼 미착용, 넘어짐, 위험구역 침입을 자동으로 감지하고 발생 시간과 추적 ID를 CSV 파일에 기록합니다. 기록된 데이터는 별도의 보고서 프로그램을 통해 사고 유형별 통계와 그래프로 확인할 수 있습니다.

현재는 로컬 영상과 실시간 스트리밍 영상을 이용해 기능을 검증했으며, 향후 DJI Mavic 3 Classic의 촬영 영상을 연동하여 실제 드론 기반 안전 관제 환경으로 확장하는 것을 목표로 합니다.

## 실행 화면

![AI 기반 실시간 드론 안전 관제 실행 화면](docs/images/main_screen.png)

## 핵심 모델 구성
> 객체 탐지: `model = YOLO("models/yolo11s.pt")`  
> 자세 분석: `pose_model = YOLO("models/yolo11n-pose.pt")`  
> 보호구 감지: `ppe_model = YOLO("models/ppe_best.pt")`

## 주요 기능

- 사람·차량·사물 실시간 탐지 및 종류별 색상 표시
  - HUMAN: 초록색
  - VEHICLE: 파란색
  - OBJECT: 노란색
- ByteTrack 기반 객체별 추적 ID 유지
- 객체의 이동 방향·속도·이동 궤적 표시
- 특정 대상 선택 및 확대 화면 제공
- 드래그 방식의 확대 영역 설정과 위치 이동
- 사용자 지정 위험구역 생성 및 침입 경고
- 안전모·안전조끼 미착용 감지
- 사람의 자세를 분석한 넘어짐 의심 감지
- 사람 수·차량 수·실시간 FPS 표시
- 위험 발생 시간과 추적 ID를 CSV 파일에 기록
- 위험구역·넘어짐·보호구 미착용 통계 그래프 출력


## 시스템 처리 흐름

1. OpenCV로 로컬 영상 또는 실시간 스트리밍 영상을 불러옵니다.  
> 핵심 코드: `cap = cv2.VideoCapture(stream_url)`

2. YOLO 모델이 영상에서 사람·차량·사물을 탐지합니다.  
> 핵심 코드: `results = model.track(frame, persist=True, tracker="bytetrack.yaml", conf=0.10, imgsz=704, device="mps")`

3. ByteTrack이 탐지된 객체마다 추적 ID를 부여하고 유지합니다.  
> 핵심 코드: `track_id = int(box.id[0]) if box.id is not None else -1`

4. 객체의 이전 위치를 저장하여 이동 방향·속도·궤적을 계산합니다.  
> 핵심 코드: `track_history[track_id].append(center)`

5. Pose 모델이 사람의 관절 위치를 분석하여 넘어짐 가능성을 판단합니다.  
> 핵심 코드: `pose_results = pose_model.predict(frame, conf=0.35, imgsz=640, device="mps")[0]`

6. PPE 모델이 안전모와 안전조끼의 착용 여부를 검사합니다.  
> 핵심 코드: `ppe_results = ppe_model.predict(frame, conf=0.25, imgsz=640, device="mps")[0]`

7. 사람의 발 위치가 사용자가 설정한 위험구역에 들어왔는지 확인합니다.  
> 핵심 코드: `if dx1 <= foot_x <= dx2 and dy1 <= foot_y <= dy2:`

8. 감지된 위험 상황의 시간과 추적 ID를 CSV 파일에 기록합니다.  
> 핵심 코드: `log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{track_id}\n")`

9. 보고서 프로그램이 CSV 기록을 집계하여 통계 그래프로 출력합니다.  
> 핵심 코드: `plt.bar(labels, values)`

## 사용 기술

- Python
- OpenCV
- Ultralytics YOLO11
- ByteTrack
- YOLO11 Pose
- Custom PPE Detection Model
- Matplotlib
- CSV
- yt-dlp

## 파일 구성

```text
linux-study/
├── drone_tracker.py              # 실시간 안전 관제 메인 프로그램
├── models/
│   ├── yolo11s.pt                # 객체 탐지 모델
│   ├── yolo11n.pt                # 경량 객체 탐지 모델
│   ├── yolo11n-pose.pt           # 자세 추정 모델
│   └── ppe_best.pt               # 보호구 감지 모델
├── data/
│   ├── fall_events.csv           # 넘어짐 의심 기록
│   ├── danger_events.csv         # 위험구역 침입 기록
│   └── ppe_events.csv            # 보호구 미착용 기록
├── reports/
│   ├── fall_report.py            # 넘어짐 통계 보고서
│   ├── danger_report.py          # 위험구역 침입 통계 보고서
│   └── ppe_report.py             # 보호구 미착용 통계 보고서
├── videos/                       # 기능 검증용 영상
├── docs/
│   └── images/                   # README용 실행 화면
├── requirements.txt              # 필수 라이브러리 목록
└── README.md                     # 프로젝트 설명 및 실행 방법
```

## 설치 및 실행 방법

### 1. 가상환경 생성
```bash
conda create -n vision-ai python=3.11 -y
conda activate vision-ai
```

### 2. 필수 라이브러리 설치
```bash
pip install -r requirements.txt
```

### 3. 메인 프로그램 실행
```bash
python drone_tracker.py
```

### 4. 통계 보고서 실행
```bash
python reports/fall_report.py
python reports/danger_report.py
python reports/ppe_report.py
```
프로그램 실행 화면에서 `q` 또는 `ESC` 키를 누르면 종료됩니다.

## 화면 조작 방법

- `SELECT` : 화면의 객체를 선택하고 추적 ID를 지정합니다.
- `ZOOM` : 마우스로 영역을 드래그하여 별도의 확대 화면을 표시합니다.
- `MOVE` : 설정한 확대 영역의 위치를 이동합니다.
- `RESET` : 선택 대상, 확대 영역, 위험구역을 초기화합니다.
- `DANGER` : 마우스로 드래그하여 감시할 위험구역을 설정합니다.
- `q`, `ESC` : 프로그램을 종료합니다.

### 객체 표시 색상

- 초록색 박스: 사람(HUMAN)
- 파란색 박스: 차량(VEHICLE)
- 노란색 박스: 기타 사물(OBJECT)
- 빨간색 영역: 사용자가 지정한 위험구역
- 자홍색 선: 객체의 이동 궤적

## 이벤트 기록 구조

감지된 위험 상황은 종류별 CSV 파일에 자동으로 저장됩니다. 동일한 추적 ID의 같은 위험 상황은 중복 기록을 줄이기 위해 10초 간격으로 기록됩니다.

### 넘어짐 기록 — `fall_events.csv`

| 항목 | 설명 |
|---|---|
| `time` | 넘어짐이 감지된 날짜와 시간 |
| `track_id` | 넘어짐이 감지된 사람의 추적 ID |

### 위험구역 침입 기록 — `danger_events.csv`

| 항목 | 설명 |
|---|---|
| `time` | 위험구역 침입이 감지된 날짜와 시간 |
| `track_id` | 위험구역에 들어온 사람의 추적 ID |

### 보호구 미착용 기록 — `ppe_events.csv`

| 항목 | 설명 |
|---|---|
| `time` | 보호구 미착용이 감지된 날짜와 시간 |
| `track_id` | 작업자의 추적 ID |
| `missing_item` | `NO_HARDHAT` 또는 `NO_SAFETY_VEST` |


## 문제 해결 및 개선 과정

### 1. 탐지 정확도와 처리 속도의 균형

고해상도 영상에서 사람이 많아지면 객체 탐지와 화면 조작이 느려지는 문제가 발생했습니다. 입력 크기를 480, 640, 704로 변경하며 테스트한 결과, 탐지 성능과 처리 속도의 균형이 가장 적절한 `imgsz=704`를 적용했습니다.
> 핵심 코드: `model.track(frame, persist=True, tracker="bytetrack.yaml", imgsz=704, device="mps")`

### 2. 위험구역 설정 방식 개선

마우스 클릭이 정확하게 인식되지 않는 문제를 해결하기 위해 위험구역 설정 방식을 클릭 방식에서 드래그 방식으로 변경했습니다. 마우스를 누른 지점부터 놓은 지점까지를 위험구역으로 지정하도록 구현했습니다.
> 핵심 코드: `danger_box = (x1, y1, x2, y2)`

### 3. 중복 경고 기록 방지

같은 작업자의 동일한 위험 상황이 프레임마다 반복 기록되는 문제를 해결하기 위해 추적 ID와 위험 유형별 마지막 기록 시간을 저장했습니다. 동일한 사건은 10초 간격으로만 CSV 파일에 기록됩니다.
> 핵심 코드: `if now - ppe_last_logged.get((track_id, item), 0) >= 10:`

### 4. 위험구역 침입 판정 개선

사람의 전체 박스가 아닌 바운딩 박스 하단 중앙의 발 위치를 기준으로 위험구역 진입 여부를 판단하도록 개선했습니다. 이를 통해 사람이 위험구역 근처에 있을 때 발생하는 잘못된 경고를 줄였습니다.
> 핵심 코드: `if dx1 <= foot_x <= dx2 and dy1 <= foot_y <= dy2:`

### 5. 확대 화면 품질 개선

선택한 객체의 확대 화면이 흐리게 보이는 문제를 해결하기 위해 확대 보간 방식을 변경하고 선명화 처리를 추가했습니다. 확대 영역을 이동할 수 있는 MOVE 기능도 구현하여 관제 편의성을 높였습니다.
> 핵심 코드: `zoom = cv2.resize(zoom, (320, 240), interpolation=cv2.INTER_CUBIC)`

## 향후 개선 계획
- DJI Mavic 3 Classic 촬영 영상과 실제 드론 환경 연동
- 보호구 감지용 학습 데이터 추가 및 탐지 정확도 향상
- 객체가 많은 영상에서의 실시간 처리 속도 개선
- 사람 출입 인원과 차량 통행량을 집계하는 통계 기능 개발
- 기능별 코드 분리를 통한 프로그램 구조 개선