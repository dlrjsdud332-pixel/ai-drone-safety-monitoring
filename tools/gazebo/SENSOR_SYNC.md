# X500 IMU timestamp / BARO TIMEOUT 조사

## 실제 확인한 증거

lsof의 열린 Gazebo server_console.log를 통해 조사 시점에 아래 서버들이 실행 중인 것을 확인했다.

| PID (조사 시점) | 시작 시각 | 열린 월드 |
|---|---|---|
| 72548 | 18:38 | camera_probe.sdf |
| 73178 | 18:56 | x500_camera.sdf (이전 성공 실행) |
| 74181 | 19:31 | industrial/worker_only.sdf |
| 74435 | 19:39 | x500_camera.sdf (현재 실행) |

뒤의 X500 세 서버 모두 world=x500_camera, model=x500_mono_cam_0, 동일 IMU·기압계·카메라
토픽을 광고한 로그가 있다. 74181과 74435에는 `Found additional publishers on /clock`
경고가 있다. 이전 두 X500의 터미널 FD는 revoked 상태인데 프로세스는 남아 있었다.
프로세스 명령행/환경을 ps로 읽는 것은 차단됐으므로 각 실행의 실제 GZ_PARTITION은 미확인이다.
서버들이 같은 partition에 있으면 동일한 world clock과 센서 토픽이 서로 다른 시뮬레이션
시각을 발행한다. **중복 서버가 현재 오류의 가장 강한 원인 후보다.**

PX4 소스 확인 결과:
- GZBridge::clockCallback은 `/world/<world>/clock`의 sim 시각으로 PX4 시각을 갱신한다.
- imuCallback은 메시지 stamp와 PX4 현재 시각 중 작은 값으로 accel/gyro를 갱신한다.
- VehicleIMU는 timestamp_sample이 이전 값보다 증가하지 않으면 timestamp error를 출력한다.
- VehicleAirData의 센서 timeout은 300ms다.
따라서 다른 시각의 clock/센서 스트림 혼합은 증상과 부합한다. 다만 이 세션에서 실제
clock/IMU 패킷을 수신해 역행을 측정하지 못했으므로 원인 해결 완료라고 판정하지 않았다.

PX4 중복은 별도 미확인이다. lsof에서 현재 PX4 프로세스 상세를 얻지 못했고
/tmp/px4-sock-0이 존재한다. 소켓 파일 존재만으로 프로세스 생존/중복을 단정할 수 없다.
QGC는 lsof에서 UDP 14550을 수신 중이었다. 이것만으로 차량 연결 성공을 뜻하지 않는다.

## 기존 성공 구성과 비교

카메라 수정 전후 확장 SDF에서 확인한 센서:
IMU 250Hz, barometer 50Hz, magnetometer 100Hz, navsat 30Hz, camera 10Hz.
차이는 camera sensor pose `0 0 0 0 0 0` → `0.22 0 0 0 0 0`뿐이다.
센서/모델 파일은 이번 조사에서 수정하지 않았다. 카메라 위치, 640×480/10Hz, 화각도 유지했다.
원래 비교에서 collision/inertial/joint/plugin 동등성을 확인했고 이번에는 해당 모델들의
해시가 바뀌지 않았음을 다시 확인했다.

실행 스크립트의 소스 기본값은 서로 같다:
GZ_IP=127.0.0.1, partition=safety_x500_camera, world=x500_camera,
model=x500_mono_cam_0, PX4_SYS_AUTOSTART=4010, standalone=1, instance=0.
partition은 기존처럼 환경변수로 지정할 수 있으므로 각 터미널에 명시하는 것이 좋다.
카메라 변경 이후 가장 큰 실제 실행 차이는 예전 X500 서버가 남은 채 새 X500 서버들이
추가로 실행됐다는 점이다.

## 최소 변경

- `run_x500_camera.sh`: 같은 world/model의 clock/센서 발행자가 있으면 중복 시작 거부.
- `run_x500_sitl.sh`: PX4 시작 전 clock/IMU/barometer/camera 발행자 각 1개,
  clock 진행·역행, 센서 timestamp 중복·역행, 기압계 300ms 초과 간격과 최근 수신 확인.
- 새 `check_x500_sync.py`: 위 읽기 전용 검사. 제한 환경에서 discovery가 불가능하면
  "발행자 없음"으로 오판하지 않고 실패로 종료한다. 자체 worker는 시간 제한을 두었다.
- 새 `test_x500_sync.py`: 판단 로직 오프라인 테스트 5개.

센서 설정, PX4 소스/파라미터, 물리/비행 설정, AI/UI, OGRE2 Metal 설정은 바꾸지 않았다.
이번 수정 전 스크립트 백업은 diagnostics/gazebo/sensor_sync/backups에 있다.

## 일반 터미널에서 실제 복구·검증

1. 현재 PX4 콘솔에서 `shutdown`으로 해당 SITL을 정상 종료한다.
2. 기존 X500 Gazebo 터미널들을 Ctrl-C로 종료한다. 터미널이 사라진 서버가 남으면
   `ps -axo pid,ppid,lstart,command` 또는 `lsof -nP -a -c ruby -d cwd`로 재확인하고
   **확인한 X500 PID만** `kill -TERM <PID>`로 종료한다.
   위 표의 PID는 조사 시점 값이며 나중에 그대로 재사용하지 않는다.
   probe/QGC/다른 작업까지 일괄 pkill하지 않는다. lock/socket 파일도 삭제하지 않는다.
3. 시간 기준이 바뀐 뒤에는 이전 PX4를 계속 사용하지 않고 Gazebo와 PX4를 한 쌍으로
   새로 시작한다. 비행 명령이나 timeout 파라미터 변경으로 오류를 가리지 않는다.

각 터미널에 다음을 명시한다:

```sh
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_x500_camera
export GAZEBO_PYTHON="$HOME/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python"
```

터미널 A:

```sh
zsh tools/gazebo/run_x500_camera.sh
```

중복 거부가 나오면 기존 X500 발행자를 먼저 정리한다. 시작 전에 저장한
`diagnostics/gazebo/sensor_sync/gazebo-preflight.json`의 ok=true는 빈 토픽 확인 결과다.

터미널 B:

```sh
zsh tools/gazebo/run_x500_sitl.sh 2>&1 | tee diagnostics/gazebo/sensor_sync/px4-terminal.log
```

시작 전 px4-preflight.json에서 publisher_counts가 모두 1, ok=true인지 확인한다.
PX4 콘솔에서 다음도 확인한다:

```
gz_bridge status
listener sensor_accel -n 5
listener sensor_gyro -n 5
listener sensor_baro -n 5
```

timestamp가 증가하는지, IMU 오류와 BARO TIMEOUT이 계속 재발하는지 확인한다.
QGC의 위치/자세/배터리 갱신도 확인한다. preflight 통과는 PX4 오류 해소 증거가 아니라
시작 직전의 Gazebo 발행 상태 확인이므로 실제 PX4 로그 검증이 추가로 필요하다.
오류가 남으면 PX4 로그의 current/previous timestamp 값과 다음 10초 관측 결과를 비교한다:

```sh
"$GAZEBO_PYTHON" tools/gazebo/check_x500_sync.py --role px4 --seconds 10 \
  --output diagnostics/gazebo/sensor_sync/after-restart.json
```

**센서 오류 해소 확인 후에만** 터미널 C에서 RGB를 검사한다:

```sh
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/sensor_sync/rgb
```

rgb/frame.ppm을 열어 좌우 날개가 사라졌는지 직접 확인한 뒤 관제에 연결한다:

```sh
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/sensor_sync/rgb \
  --tracker-python "$(command -v python)"
```

중복 서버가 남아 있으면 예전 카메라의 RGB도 동일 토픽으로 섞일 수 있어 날개 제거 검증도
신뢰할 수 없다. 프레임 수신 PASS 자체를 날개 제거 성공으로 해석하지 않는다.

## 이번 세션 결과 / 한계

중복 X500 서버 및 광고 토픽·추가 clock 경고를 로그로 확인했다.
이전 서버 73178/74181에 SIGTERM을 시도했지만 `Operation not permitted`로 거부됐다.
추가 서버를 새로 실행하지 않았으며 현재 서버/PX4/QGC를 중지했다고 주장하지 않는다.
실시간 Transport 검사는 로컬 소켓 바인딩 `Operation not permitted`로 실패했다.
따라서 publisher 수·시각 역행의 실측, 센서 오류 해소, 수정 후 RGB의 날개 제거는 미검증이다.
정적 모델 비교·구문 검사·오프라인 테스트 5개는 통과했다.

근거는 diagnostics/gazebo/sensor_sync/의 process-evidence.json, server-*.log,
model-comparison.json, cleanup-attempt.jsonl, live-probe.json, offline-tests.log에 보존했다.
