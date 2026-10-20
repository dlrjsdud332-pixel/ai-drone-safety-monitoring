# AI Drone Safety Monitoring — 개발 프로토타입 v0.1

macOS Apple Silicon용 PySide6 관제 프로그램입니다. 현재 유지하는 메인 프로그램은 **`legacy/drone_tracker.py`**입니다. 사람·차량 YOLO/ByteTrack, PPE·자세 추정, FIRE·SMOKE 경고, 이벤트 JSON/스냅샷, PX4 텔레메트리 및 미션 상태 표시를 포함합니다. 루트의 `main.py`와 `drone_tracker.py`는 다른 실행 경로이므로 아래 명령과 혼용하지 마세요. 루트 `drone_tracker.py`는 기존 독립 실행 코드를 그대로 보존했으며, 0바이트 업로드 후보로 덮어쓰지 않았습니다.

## v0.1 범위와 한계

기존 YOLO·ByteTrack·CROWD·FIRE·SMOKE 운영 설정을 유지한 개발 프로토타입입니다. 추가 AI 성능 실험은 종료했으며, 실제 산업현장 성능 인증이나 안전 보장을 의미하지 않습니다. 기존 UI와 PX4·Gazebo 연동 코드는 보존합니다.

- 군중 밀집 영역·NORMAL/CAUTION/HIGH 분석은 `tools/ai/crowd_regions.py`의 **실험 기능**이며 운영 경고에 통합하지 않았습니다. 원거리 사람 고해상도·타일 추론도 미적용입니다.
- 원거리/주간 화재 HSV·ROI 재탐지는 일부 부분 영역 추가 검출이 있었으나 실시간 비용과 검증 데이터 한계로 **운영 미적용**입니다.
- 사용자 검수 JSON이 없어 화재 30프레임의 정식 정확도 평가는 미완료입니다.
- 산업현장 실험 테스트에는 **실패 1건·오류 1건**이 남아 있습니다. [검증 보고서](docs/VALIDATION_REPORT.md)를 확인하세요.
- 모델·영상·이벤트·제3자 다운로드 에셋은 새 업로드 묶음에서 제외합니다. 기존 원격에서 이미 추적 중인 파일은 별도 검토 대상입니다.

## 설치

검사에 사용한 환경은 Python 3.10.21 / macOS Apple Silicon입니다. 기존 환경에서 테스트했으며 새 환경 설치는 미검증입니다.

```sh
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-macos-tested.txt
# 실시간 스트림 주소 처리에는 ffmpeg가 필요합니다.
brew install ffmpeg
```

`requirements.txt`는 일반 의존성 목록, `requirements-macos-tested.txt`는 확인한 환경의 직접 의존성 버전입니다. Gazebo Transport는 이 pip 환경에 섞지 않고 기존 PX4 macOS Pixi Python에서 별도 worker로 실행합니다.

필요한 모델은 사용 권한을 확인한 뒤 `models/`에 배치합니다. 가중치 파일과 테스트 영상은 기본 Git 업로드 대상에서 제외됩니다.

| 파일 | 역할 |
|---|---|
| `yolo11s.pt` | 사람·차량 / ByteTrack |
| `yolo11n-pose.pt` | 자세 추정 |
| `ppe_best.pt` | 안전모·안전조끼 |
| `fire_smoke_best.pt` | smoke=0 / fire=1 |

PPE 및 화재 가중치의 원 학습 데이터/배포 허가는 아직 확인되지 않았습니다. 임의 가중치 교체나 자동 다운로드는 하지 않습니다. [라이선스 검토](docs/LICENSE_REVIEW.md)를 참고하세요.

## 파일 영상 실행

```sh
python legacy/drone_tracker.py /path/to/local-video.mp4 --device cpu --flight-adapter SIM
```

파일 입력을 유지합니다. 지원 장치에서는 `--device mps`를 사용할 수 있으나 이번 검사에서는 CPU만 확인했습니다. O: 로컬 영상, U: 스트림, P: PPE, H: FIRE/SMOKE 표시, Q: 종료입니다. 이벤트는 `data/events/`, 큐는 `data/transmission_queue/`, RISK 기록은 `data/alerts/`에 저장됩니다. 이 디렉터리에는 촬영·위치·검수 정보가 포함될 수 있어 Git에서 제외합니다.

## 기존 X500 / Gazebo 연동

기존 PX4 소스와 정상 빌드, Gazebo Sim 8/Pixi 환경, QGroundControl을 별도로 준비해야 합니다. 이 저장소에 PX4 본체는 포함하지 않습니다. 기존 OGRE2 Metal 설정을 보존했습니다. 각 터미널에서 같은 partition을 사용하고 중복 서버를 실행하지 마세요.

```sh
export PX4_SOURCE_DIR="$HOME/Desktop/PX4-Autopilot"
export GZ_PARTITION=safety_x500_camera
export GAZEBO_PYTHON="$PX4_SOURCE_DIR/Tools/setup/macos/.pixi/envs/default/bin/python"
# 터미널 A
zsh tools/gazebo/run_x500_camera.sh
# 터미널 B: 같은 환경변수 설정 후
zsh tools/gazebo/run_x500_sitl.sh
# 터미널 C: 먼저 실제 프레임 수신 검사
python tools/gazebo/verify_x500_camera.py --count 10 --timeout 20
```

검사 후 실제 토픽이 확인된 경우 관제 프로그램에 연결합니다.

```sh
python legacy/drone_tracker.py   gazebo:///world/x500_camera/model/x500_mono_cam_0/link/camera_link/sensor/camera/image   --flight-adapter PX4 --device cpu
```

검증된 테스트 카메라는 `gazebo:///safety/probe/image`입니다. 자세한 환경 설정은 [Gazebo 문서](tools/gazebo/README.md)를 참고하세요. 이번 업로드 준비 세션의 실제 비행/QGC GUI/미션 진행률 재검증은 완료되지 않았습니다.

## 오프라인 테스트

```sh
python test_agent_safety.py
python tools/ai/test_alarm_logic.py
python tools/ai/test_fire_dataset.py
python tools/ai/test_fire_review.py
# Gazebo 패키지가 있는 전용 환경에서:
"$GAZEBO_PYTHON" -m unittest discover -s tools/gazebo -p 'test_*.py'
```

관제 UI를 화면 없이 검사하려면 별도의 새 출력 폴더를 사용합니다. offscreen 성공을 실제 macOS 창/비행 성공으로 간주하지 않습니다.

```sh
python tools/ai/validate_gui.py --offscreen --output diagnostics/gui_new   --seconds 40 /path/to/local-video.mp4
```

## 화재·연기 사용자 검수와 기준 평가

검수 후보 30프레임과 원본 영상/JSON은 개인정보·출처 검토 때문에 업로드하지 않습니다. 로컬 검수 도구는 낮 화재 10프레임부터 자동 FIRE/SMOKE 후보와 부분 초안을 편집하게 하며, 사용자 완료 프레임만 평가에 사용합니다. 저장된 HTML 페이지는 검수 결과 JSON을 대체할 수 없습니다.

사용자 완료 JSON을 확보하지 못했으며 검수 및 성능 실험은 보류·종료 상태입니다. 따라서 **30프레임 기준 정확도 평가, 낮/석양/야간 미탐지율·오탐률은 미완료**입니다. 기존 미검수 라벨을 정답으로 확정하지 않았습니다.

실제 검수 JSON을 확보한 뒤 로컬 데이터셋에서:

```sh
python tools/ai/prepare_fire_review.py import --dataset diagnostics/fire_dataset_v1   --review-json /path/to/fire_smoke_human_review.json
python tools/ai/prepare_fire_review.py evaluate --dataset diagnostics/fire_dataset_v1
```

반자동 후보/화면 재생성에는 로컬 원본 데이터와 manifest가 필요합니다. 업로드된 소스만으로 비공개 검수 자료가 자동 복원되지는 않습니다. 학습 명령은 실행하지 않았습니다.

이번 검사 결과와 미완료 항목은 [최종 검증 보고서](docs/VALIDATION_REPORT.md), 업로드 대상/제외 정책은 [업로드 준비 보고서](docs/UPLOAD_REVIEW.md)에 기록합니다. 기존 이력을 보존한 clone의 `release/prototype-v0.1` 브랜치에 후보 99개 중 98개를 반영했습니다. HEAD 대비 추가 84개·수정 4개·동일 후보 10개이며 루트 `drone_tracker.py` 후보 1개는 보류했습니다. 문서 후속 수정도 이 집계 안에 포함됩니다. stage·commit·push는 실행하지 않았습니다. 준비 후보와 실제 반영 범위는 [파일 목록](docs/UPLOAD_FILES.md)에 정리했습니다. 원격 이력 보존 절차와 승인 전 남은 항목은 [업로드 준비 보고서](docs/UPLOAD_REVIEW.md)를 확인하세요.

## v0.1 핵심 오프라인 회귀 검사

```sh
python tools/release/check_prototype.py \
  --gazebo-python "$GAZEBO_PYTHON" \
  --output diagnostics/prototype_checks_new
python tools/release/audit_upload.py --output diagnostics/upload_audit_new
```

검사 도구는 기존 단위 테스트만 실행하며 추가 추론 성능 실험·모델 학습·비행·Git push를 실행하지 않습니다. 알려진 산업현장 실패/오류가 포함되므로 전체 테스트 명령은 종료 코드 1을 반환합니다. AI용 Python과 Gazebo 전용 Python이 모두 필요합니다.
