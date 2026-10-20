# X500 1인칭 카메라 프로펠러 간섭 수정

기존 `diagnostics/gazebo/x500/frame.ppm`에서 검은 날개를 확인했다.
X500 모델·STL 형상과 전방 로터 위치를 대조하면 앞쪽 rotor_0 / rotor_2의
1345_prop_ccw.stl / 1345_prop_cw.stl 프로펠러가 원인으로 판단된다.

원래 장착 pose는 `0.12 0.03 0.242 0 0 0`, camera sensor pose는 모두 0이다.
앞쪽 로터 X=0.174m에 STL scale/visual offset을 적용한 회전 반경은 약 0.14677m로,
프로펠러가 X≈0.32077m까지 나올 수 있다. 기존 카메라 X=0.12m보다 앞이다.

`mono_cam_clear`는 기존 mono_cam의 sensor pose만 `0.22 0 0 0 0 0`으로 바꾼다.
광학 중심의 X는 0.34m가 되어 계산된 프로펠러 회전 범위보다 약 1.9cm 앞에 놓인다.
Y/Z, 카메라 방향, FOV 1.74rad, clip 0.1–3000m, 640×480, 10Hz는 그대로다.
카메라 housing/link/inertial을 옮기지 않아 기체의 질량·무게중심·조인트도 그대로다.

`x500_mono_cam_clear`는 upstream x500을 그대로 포함하고 카메라 include만
mono_cam_clear로 바꾼다. 월드의 실제 모델명은 기존 `x500_mono_cam_0`를 유지한다.
PX4 원본 디렉터리는 변경하지 않고 프로젝트 내부에 모델 사본을 두었다.

수정 파일:
- `tools/gazebo/x500_camera.sdf`: X500 include URI 한 줄 변경.
- `tools/gazebo/run_x500_camera.sh`: 로컬 모델 리소스 경로 한 줄 추가.
- `tools/gazebo/models/{mono_cam_clear,x500_mono_cam_clear}/{model.sdf,model.config}` 추가.

백업: `diagnostics/gazebo/camera_clearance/backups/`.
원본 RGB 사본: `diagnostics/gazebo/camera_clearance/before.ppm`.

## 적용

실행 중인 Gazebo/SITL을 각 콘솔에서 정상 종료한 뒤 프로젝트 루트에서 다시 실행한다.
이미 생성된 기체에는 파일 변경이 자동 적용되지 않는다.

```sh
# 터미널 A
zsh tools/gazebo/run_x500_camera.sh

# 터미널 B
zsh tools/gazebo/run_x500_sitl.sh

# 터미널 C: 기존 관제 Python 환경
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_x500_camera
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/camera_clearance/live-check
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/camera_clearance/live-check --tracker-python "$(command -v python)"
```

독립 수신으로 저장된 live-check/frame.ppm 및 실제 관제 화면에서 좌우 날개가
사라졌는지 확인한다. QGC 연결·위치/자세 갱신 및 비행 시 영상도 확인한다.
프레임 수신 PASS만으로 날개 제거 성공을 판정하지 않는다.
이 변경은 위 x500_camera 월드에 적용되며 다른 실험 월드는 별도다.

## 확인한 범위

- 설치된 gz sdf로 수정 전/후 월드를 확장해 로컬 모델 참조 해석을 확인했다.
- sensor pose 이외의 센서 설정 동일; 640×480/10Hz/FOV/방향 유지.
- 확장된 모든 collision/inertial/joint/plugin이 수정 전과 동일함을 비교했다.
- upstream PX4 모델, tracker, camera_source, SITL launcher의 SHA256 미변경 확인.
- 수정된 월드는 `gz sdf -k`에서도 Valid, exit 0을 확인했다.
- 별도 partition에서 실제 RGB 수신을 시도했지만 Gazebo가
  `InitializeSockets() Error: Operation not permitted`로 실패했다.
  runtime-result.json의 rgb_verified=false이며 수정 후 RGB·비행 회귀 검증은 미완료다.

원상 복귀는 backups의 x500_camera.sdf와 run_x500_camera.sh를 원래 위치로 복사한 뒤
Gazebo/SITL을 재시작한다. upstream PX4 파일의 복원은 필요하지 않다.
