# PX4 X500 장착 카메라: 준비 완료, 실제 수신 검증 대기

## 조사 결과

설치된 `~/Desktop/PX4-Autopilot` 소스 기준:

- `Tools/simulation/gz/models/x500_mono_cam/model.sdf`는 x500과 mono_cam을
  merge include하며 `CameraJoint` fixed joint로 base_link와 camera_link를 연결한다.
  장착 pose는 `0.12 0.03 0.242 0 0 0`이다. 월드 고정 카메라가 아니다.
- `models/mono_cam/model.sdf`: camera_link / sensor camera / type camera,
  640×480, 10 Hz, 수평 FOV 1.74 rad, clip 0.1–3000 m, always_on.
  명시적 topic/format은 없다. 설치된 sdformat14의 1.9/camera.sdf에서
  image format 기본값은 `R8G8B8`다. **실제 메시지의 픽셀 포맷·step은 아직 미확인**이다.
- 해당 airframe은 `4010_gz_x500_mono_cam`이다.
- `px4-rc.gzsim`은 PX4_GZ_STANDALONE=1과 PX4_GZ_MODEL_NAME을 지정하면
  Gazebo를 다시 실행하거나 모델을 중복 생성하지 않고 기존 모델에 attach한다.
- 설치된 `~/.gz/sim/8/server.config`의 Sensors에는 `ogre`가 남아 있다.
  파일을 수정하지 않고 별도 월드에 Sensors/render_engine=ogre2를 명시했다.
- PX4 MAVLink 초기화 소스 기준 instance 0의 GCS 로컬 포트는 18570,
  onboard는 14580→14540이다. GCS 기본 원격 포트는 14550이다.
  이는 구성 조사 결과이며 실제 QGC 연결 확인 결과가 아니다.

이번 월드에서 예상하는 자동 카메라 토픽:

```
/world/x500_camera/model/x500_mono_cam_0/link/camera_link/sensor/camera/image
```

위 주소는 예상값이다. 검증 도구는 topic list에서 해당 모델/센서의 **실제 토픽을
검색**하고 publisher type이 gz.msgs.Image인지 검사한다. probe 토픽이나 다른 모델
영상으로 통과시키지 않는다.

## 추가한 파일 / 보존 범위

- `x500_camera.sdf`: 기존 safety_test 장면 복사본 + 8개 명시적 world system
  (Physics/UserCommands/SceneBroadcaster/Sensors/Imu/AirPressure/Magnetometer/NavSat)
  + upstream x500_mono_cam 모델 include. 월드명 x500_camera, 모델명 x500_mono_cam_0.
- `run_x500_camera.sh`: 기존 run_camera_probe.sh를 그대로 호출한다.
  OGRE/OGRE2 리소스 경로와 **OGRE2 Metal** 서버 옵션을 재사용하며
  기본 partition만 safety_x500_camera로 분리한다.
- `run_x500_sitl.sh`: 기존 SITL 바이너리로 airframe 4010을 실행하고 위 모델에 attach.
  파라미터와 로그는 프로젝트 diagnostics/gazebo/x500/sitl에 별도로 저장한다.
  PX4 재빌드, 기존 파라미터 수정, 중복 모델 spawn을 수행하지 않는다.
- `verify_x500_camera.py`: 토픽 발견 → Image publisher 검사 → 기존 BGR 어댑터로
  640×480 프레임 10개 수신 → 필요 시 이동 관찰 → 요청한 경우에만 관제 실행.
- `test_verify_x500_camera.py`: 토픽 구분, 잘못된 타입 거부, BGR snapshot,
  프레임 수신 전 관제 실행 방지, 발견한 토픽으로 관제 연결 테스트.

기존 tracker/camera_source/probe launcher, PX4 원본 모델과 월드, 사용자 server.config는
변경하지 않았다. 수정 전 상태 사본은 `diagnostics/gazebo/x500/backups/`,
해시는 `diagnostics/gazebo/x500/baseline-sha256.json`에 있다.
PySide6/YOLO/PPE/화재·연기/안전 경고/이벤트/PX4 텔레메트리/미션 진행률 코드는
이번 작업에서 변경하지 않았다. 해당 기능들의 실제 회귀 검증은 별도다.

## 실행 순서: 일반 macOS 터미널, 프로젝트 루트

기존 PX4 instance 0이 실행 중이면 먼저 해당 실행을 정상 종료한다.
probe는 다른 partition이므로 보존되어 있지만 부하를 줄이려면 중지해도 된다.
아래 절차는 모두 같은 partition을 사용해야 한다. 기본값은 safety_x500_camera다.
설치 경로가 다르면 각 터미널에서 PX4_SOURCE_DIR을 지정한다.

### 1. Gazebo와 X500 카메라만 먼저 실행

터미널 A:

```sh
zsh tools/gazebo/run_x500_camera.sh 2>&1 | tee diagnostics/gazebo/x500/server-terminal.log
```

터미널 B에서 기존 관제 Python 환경 활성화 후:

```sh
conda activate vision-ai
python tools/gazebo/verify_x500_camera.py 2>&1 | tee diagnostics/gazebo/x500/frames-terminal.log
```

vision-ai는 README의 예시 이름이다. 기존 관제가 성공했던 실제 환경을 사용한다.
이 단계에는 numpy가 필요하며 PySide6/YOLO를 실행하지 않는다.
검증 도구가 GZ_IP=127.0.0.1, GZ_PARTITION=safety_x500_camera 및 기본
GAZEBO_PYTHON을 설정한다. 이미 해당 환경변수가 설정되어 있다면 기존 값을
사용하므로 probe용 partition이 남아 있지 않은지 확인한다. 필요하면 명시한다:

```sh
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_x500_camera
export GAZEBO_PYTHON="$HOME/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python"
```

필수 확인: verification.json의 verified=true, 실제 topic, 10개 프레임과 FPS,
worker의 실제 RGB_INT8/step/bytes 출력, frame.ppm의 정상 장면.
정상이라면 보통 RGB_INT8(3), step=1920, bytes=921600이 예상된다.
통과하지 않으면 **관제 연결 단계로 넘어가지 않는다**.

### 2. PX4 SITL attach와 QGroundControl 확인

프레임 검사가 통과한 뒤 터미널 C:

```sh
zsh tools/gazebo/run_x500_sitl.sh
```

설치된 QGroundControl을 실행하고 UDP 14550 자동 연결에서 차량이 나타나는지,
위치/자세/배터리와 연결 상태가 갱신되는지 확인한다.
PX4 콘솔에서도 다음 상태를 확인한다:

```
gz_bridge status
mavlink status
listener vehicle_global_position -n 1
```

기존 읽기 전용 텔레메트리 검사를 별도로 사용할 수 있다:

```sh
python px4_telemetry_test.py --port 14540 --timeout 20
```

확인 후 Ctrl-C로 종료한다. 이 검사/이동 관찰/관제는 같은 UDP 14540 수신 포트를
사용하므로 동시에 실행하지 않는다. QGC의 14550 수신은 별도다.
PX4 heartbeat 수신만으로 QGC UI 연결 성공을 판정하지 않는다.

### 3. 드론 이동과 영상 변화 동시 관찰 (관제 실행 전)

pymavlink가 설치된 기존 관제 Python에서:

```sh
python tools/gazebo/verify_x500_camera.py --motion 60
```

관찰 시작 메시지 후 QGC에서 이 SITL 드론을 이륙/이동시킨다.
관찰 도구는 비행 명령을 보내지 않는다. PX4 heartbeat와 최근 GLOBAL_POSITION_INT,
카메라 프레임을 함께 관찰하며 기준 위치에서 1m 이상 이동하고 동일 시점의
축소 BGR 영상 평균 절대 차이가 2/255 이상이면 관찰 결과를 저장한다.
고정 조인트는 원본 SDF 조사로 확인되며, 이 관찰은 이동과 영상 변화의 동시
발생을 확인한다. 단순 영상 변화만으로 비행 성공을 판정하지 않는다.
`motion-before.ppm`, `motion-after.ppm`에서 기대한 시야 변화도 직접 확인한다.
검증이 끝난 뒤 QGC로 착륙한다. 자동 이륙/이동/착륙 코드는 추가하지 않았다.

### 4. 프레임 수신 통과 후 기존 관제에 연결

터미널 B에서:

```sh
python tools/gazebo/verify_x500_camera.py --tracker-python "$(command -v python)"
```

이 명령은 실제 토픽과 10프레임 수신을 다시 확인한 뒤, 발견한 주소로 다음과
동등한 관제 명령을 실행한다. 토픽 검사나 수신이 실패하면 관제를 실행하지 않는다:

```
python legacy/drone_tracker.py gazebo:///world/x500_camera/model/x500_mono_cam_0/link/camera_link/sensor/camera/image --flight-adapter PX4 --px4-telemetry-port 14540
```

관제에서 X500 영상/RX FPS/AI FPS, QGC 이동에 따른 시야와 PX4 위치·고도 갱신,
안전 경고 및 미션 패널을 확인한다. 기존 관제의 PX4 어댑터는 수신 전용이며
QGC에서 수행한 비행을 새로운 관제 미션 진행률로 자동 등록하지는 않는다.
기존 probe 연결 방법과 U 키를 이용한 영상 교체도 유지된다.

## 이번 세션 검증 결과

- Python 구문 검사, zsh 구문 검사, 월드 XML/8개 system/OGRE2/모델 include 검사 통과.
- 기존 어댑터 테스트 6개 + 새 연결 선행 검사 테스트 5개 통과.
- 백업 해시와 원본 비교: 기존 파일 변경 없음.
- Gazebo 서버와 SITL을 함께 실행하고 topic discovery를 시도했으나 샌드박스에서
  Gazebo InitializeSockets() Error: Operation not permitted,
  PX4 /tmp/px4-sock-0 바인딩 Operation not permitted로 실패.
- Gazebo에서 월드/모델 및 system 로딩은 로그로 확인했지만 topic 발행과
  실제 프레임 수신은 **미검증**. 서버 프로세스 생존을 성공으로 판정하지 않았다.
- verification.json: verified=false. 선행 검사 실패로 관제 실행하지 않음.
- QGC 연결, 이동에 따른 영상 변화, X500 관제 표시/YOLO/미션 패널은 **미검증**.
  일반적인 Applications/Downloads/Desktop 경로에서 QGC 앱을 찾지 못했지만,
  다른 경로의 설치 여부는 확인하지 않았다.
- 기존 probe 영상이 관제에 표시된 사실은 사용자 확인 결과이며 이번 X500 결과와 별개다.

근거 로그: `diagnostics/gazebo/x500/{server,sitl,camera}-attempt.log`,
`attempts.json`, `verification.json`, `gate-tests.log`.

오프라인 검사 재실행:

```sh
python -m py_compile tools/gazebo/verify_x500_camera.py tools/gazebo/test_verify_x500_camera.py
zsh -n tools/gazebo/run_x500_camera.sh tools/gazebo/run_x500_sitl.sh
"$GAZEBO_PYTHON" tools/gazebo/test_camera_source.py
"$GAZEBO_PYTHON" tools/gazebo/test_verify_x500_camera.py
```
