# Gazebo probe → legacy tracker

## 구조 및 변경 범위

기존 `Channel.read_loop()`는 OpenCV/FFmpeg로 영상을 읽고 `publish()`에서
lock으로 보호한 최신 `pending` 프레임과 sequence를 갱신한다.
`Analyzer.run()`은 새 sequence만 가져와 채널별 YOLO 추적 모델로
`process()`를 호출한다. 이 경로에 pose/PPE/fire/smoke 분석과 이벤트 저장이
연결되어 있다. Qt는 `live_snapshot()`으로 최신 원본과 분석 overlay를 표시한다.

추가한 `gazebo:///safety/probe/image` 입력은 동일한 `publish()`에 BGR ndarray를
전달한다. 파일/USB/RTSP/HTTP/YouTube 입력, 분석 모델, 이벤트 정책 및 PX4
텔레메트리 코드는 유지했다. X500 모델/월드/카메라는 변경하지 않았다.

Gazebo Python과 관제 Python의 버전/패키지 구성이 달라 별도 worker를 사용한다.
worker는 gz.transport13 / gz.msgs10으로 구독하고 실제 메시지의 enum 이름,
width/height/step/payload를 검사한다. RGB/BGR/RGBA/BGRA 8-bit 및 L8만
명시적으로 지원하고 나머지 포맷은 거부한다. 행 패딩을 제거하고 packed BGR8로
변환한다. 최초 및 metadata 변경 시 실제 값이 stderr에 출력된다.
이 환경의 probe에서 기대되는 값은 RGB_INT8(3), 640×480, step=1920,
921600 bytes다. 이번 샌드박스에서는 실제 수신 metadata를 확인하지 못했다.

Transport callback은 condition으로 보호한 최신 프레임 한 장만 교체한다.
worker 출력 루프가 길이 헤더와 BGR 데이터를 파이프로 전송하고, 기존 Channel
수신 스레드가 ndarray로 복원한다. 부모도 최신 pending 한 장을 유지하므로
추론 속도보다 빠른 수신으로 프레임 대기열이 계속 증가하지 않는다.
구독과 파이프 읽기는 Qt 스레드에서 실행하지 않는다.

5초 동안 완전한 프레임이 없거나 worker 종료/잘못된 데이터가 발생하면 worker를
정리하고 pending을 비우며 RX FPS를 0으로 바꾼다. 기존 재연결 루프가 3초 후
새 worker로 재구독한다. 마지막 분석 화면은 남을 수 있지만 상태에
`Connection error - retrying`이 표시된다. 채널 교체/종료는 stop event 및
프로세스 terminate로 처리하며 정리 시 2초 내 종료되지 않으면 kill한다.

## 일반 macOS 터미널에서 실행

프로젝트 루트에서 첫 터미널:

```sh
zsh tools/gazebo/run_camera_probe.sh
```

두 번째 터미널에서 독립 BGR 수신 검사 (GUI/YOLO 없음):

```sh
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_camera_probe
export GAZEBO_PYTHON="$HOME/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python"
"$GAZEBO_PYTHON" tools/gazebo/camera_source.py --count 10 --timeout 20
```

10개 BGR 프레임의 shape/dtype/bytes/FPS가 출력되어야 하며
`diagnostics/gazebo/bgr_frame.ppm`에서 빨간 상자의 색을 확인한다.
worker가 출력하는 실제 format/step도 확인한다. GZ_PARTITION은 서버와 같아야 한다.
Gazebo 설치 위치가 다르면 GAZEBO_PYTHON을 해당 Python으로 지정한다.

세 번째 터미널에서 기존 관제 환경으로 실행:

```sh
conda activate vision-ai
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_camera_probe
export GAZEBO_PYTHON="$HOME/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python"
python legacy/drone_tracker.py gazebo:///safety/probe/image
```

`vision-ai`는 README의 예시 환경명이다. 실제 사용 중인 PySide6/YOLO/OpenCV
환경을 사용한다. 기존 입력과 함께 사용하려면 sources에 나란히 지정한다:

```sh
python legacy/drone_tracker.py gazebo:///safety/probe/image videos/example.mp4
```

GUI에서 U 키 → 슬롯 선택 → `gazebo:///safety/probe/image`를 입력해도 된다.
PX4 텔레메트리가 필요하면 기존 `--flight-adapter PX4` 옵션을 그대로 추가한다.
현재 작업에서는 PX4/X500 카메라로 토픽을 변경하지 않는다.

## 검증 결과 및 남은 확인

- 수정 전 백업: `diagnostics/gazebo/backups/drone_tracker.before_gazebo.py`.
- Python 구문 검사 통과: tracker, camera_source, test_camera_source.
- 오프라인 테스트 6개 통과. 로그: `diagnostics/gazebo/camera-unit-tests.log`.
  변환/행 패딩/포맷/불량 메시지, subprocess 파이프와 종료,
  타임아웃 정리, 실제 Channel 클래스의 read_gazebo→publish 연결 검증.
- 독립 실제 수신 시도 실패: `diagnostics/gazebo/bgr-receive-test.log`.
  샌드박스가 로컬 소켓 바인딩을 거부했으며 5초 타임아웃 후 worker 정리됨.
  사용자에게서 전달된 기존 probe 10프레임/10 FPS 결과와 이번 검증은 별개다.
- GUI 및 YOLO 실제 실행은 미검증. 실제 관제 Python 환경에서 위 명령으로
  화면/RX/AI FPS, GUI 반응, worker metadata, 종료를 확인해야 한다.
- 서버 중단 후 오류 상태, 서버 재시작 후 Playing 복귀를 실제 환경에서 확인해야 한다.
- probe의 빨간 상자는 person/PPE/fire/smoke 탐지 성능 검증 대상이 아니다.
  이 기능들의 실제 탐지 및 PX4 텔레메트리 회귀 검증은 수행하지 않았다.

오프라인 검사 재실행:

```sh
python -m py_compile legacy/drone_tracker.py tools/gazebo/camera_source.py tools/gazebo/test_camera_source.py
"$GAZEBO_PYTHON" tools/gazebo/test_camera_source.py
```
