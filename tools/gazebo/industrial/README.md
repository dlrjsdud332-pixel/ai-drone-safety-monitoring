# 산업현장 월드 — 구성 구현, 외부 에셋 설치·렌더 검증 대기

사용자가 기존 X500+Gazebo+QGC+관제 연결 성공을 확인한 상태에서 추가한 별도 실험이다.
**이번 세션에는 실제 작업자 GLB와 콘크리트 파일을 내려받지 못했다.** 외부 DNS 제한으로
다운로드가 실패했으며 파일이 없는 상태를 숨기기 위한 대체 사람 모델은 넣지 않았다.
작업자 렌더·외형 개선·실시간 성능·YOLO 탐지 성공은 아직 확인하지 못했다.

먼저 작성한 [후보·라이선스·호환성·성능 검토](ASSET_REVIEW.md)를 참고한다.
선택은 Innerscene Drew (작업복/안전모, 배포자 CC0 표시) 한 명과 Poly Haven CC0 콘크리트다.
예상 GLB 크기는 배포 페이지 기준 22.8MB이며 실제 삼각형/재질 수는 설치 후 기록된다.
배경 구조물/펜스 메시/입자 마스크는 이 프로젝트에서 만든 원본 CC0 에셋이다.

## 보존한 구성

- `tools/gazebo/x500_camera.sdf`, tracker, 어댑터, 기존 launcher는 수정하지 않았다.
- 백업: `diagnostics/gazebo/industrial/backups/`, 해시: `baseline-sha256.json`.
- 새 월드도 world=`x500_camera`, model=`x500_mono_cam_0`를 사용한다.
  upstream x500_mono_cam include·pose·physics·좌표계·OGRE2 설정을 복사했다.
  따라서 기존 `run_x500_sitl.sh`와 기존 관제 토픽을 그대로 재사용한다.
- 카메라 원본 센서 설정은 변경하지 않았으며 RGB 640×480·10Hz 기준이다.
  실제 측정에서는 수신 프레임 크기를 재확인한다.
- Qt/YOLO/PPE/경고/이벤트/텔레메트리/미션 진행률 코드를 변경하지 않았다.
- 같은 월드명·모델명·partition을 사용하므로 원본과 새 월드를 동시에 실행하지 않는다.

## 단계별 실행 — 일반 macOS 터미널, 프로젝트 루트

기존 관제가 정상 동작했던 Python 환경을 사용한다. numpy가 필요하다.
YOLO 오프라인 검사는 그 환경의 ultralytics/OpenCV/Torch도 사용한다.
Gazebo worker/에셋 오프라인 검사에는 기존 Pixi Python을 사용할 수 있다.

각 터미널에서 같은 partition을 사용한다:

```sh
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_x500_camera
export GAZEBO_PYTHON="$HOME/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python"
```

### 0. 원본 환경 성능 저장

현재 정상 동작 중인 원본 월드에 대해, 동일한 기체 위치·방향과 UI/YOLO 부하에서:

```sh
python tools/gazebo/industrial/measure_scene.py capture baseline
```

20프레임 warmup 후 300프레임을 측정한다. 약 10Hz라면 약 32초+탐색 시간이 걸린다.
결과와 최대 10개 RGB PPM은 `diagnostics/gazebo/industrial/benchmark/baseline/`에 저장된다.
BGR 수신 FPS와 간격 p95·최대값, 가능하면 WorldStatistics의 RTF를 기록한다.
측정은 GUI/YOLO를 추가 실행하지 않으며, 별도 worker를 구독하므로 그 비용이 포함된다.

### 1. 검토한 작업자 한 명만 설치·렌더

```sh
python tools/gazebo/industrial/prepare_assets.py
```

고정된 공식 다운로드 endpoint에서 GLB를 받아 GLB 버전/길이/BIN/텍스처 형식과
삼각형 예산(최대 25만)을 검사한 뒤 `assets.lock.json`에 실제 SHA256·다운로드 시각·메시 통계를 기록한다.
기록된 SHA는 **배포자가 제공한 검증 해시가 아니라 다운로드한 파일의 관찰값**이다.
이미 설치된 파일의 hash가 바뀌면 실행을 거부하며 자동 덮어쓰기하지 않는다.
필수 확장자가 검증 범위 밖이거나 이미지가 PNG/JPEG가 아니면 중단한다.
리깅 데이터가 있더라도 이번 모델은 static visual이며 걷기 애니메이션은 적용하지 않는다.

기존 SITL과 원본 Gazebo 실행을 각 콘솔에서 정상 종료한 후:

```sh
# 터미널 A — 기본값은 worker 단계
zsh tools/gazebo/run_industrial_world.sh worker

# 터미널 B — 기존 SITL 실행 방식 유지
zsh tools/gazebo/run_x500_sitl.sh

# 터미널 C — 독립 프레임 수신, 이후 관제 연결
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/industrial/worker-verification
python tools/gazebo/verify_x500_camera.py --output diagnostics/gazebo/industrial/worker-verification --tracker-python "$(command -v python)"
```

이 월드에는 worker 한 명, 기존 바닥/조명/X500만 있고 공장/연기 효과는 없다.
작업자는 X=8m에 배치했다. 지상 카메라에서는 작업복 하단이 잘릴 수 있으므로 실제
SITL을 적절한 관측 고도·거리로 이동시켜 전신과 안전모를 확인한다. 비교 측정은 동일 pose로 한다.
GLB의 Y-up→Z-up 회전을 설정했지만 원본 파일을 확인하지 못했으므로
발 위치/1.888m 실척/방향/작업복·안전모·피부 텍스처·그림자를 첫 렌더에서 확인해야 한다.

**worker 단독 단계가 실제로 정상 표시되는 것을 확인한 뒤에만 다음 단계로 진행한다.**

```sh
python tools/gazebo/industrial/measure_scene.py capture worker
```

### 2. 산업현장 배경 추가

```sh
python tools/gazebo/industrial/prepare_assets.py --site
```

공식 Poly Haven files API에서 1K diffuse/OpenGL normal/roughness JPG URL을 조회한다.
4K/16K, HDRI, displacement는 내려받지 않는다. 색상·거칠기·노멀은 별도 SDF PBR 맵이다.
Gazebo/SITL을 정상 종료한 후 worker 실행 대신:

```sh
zsh tools/gazebo/run_industrial_world.sh site
```

다른 터미널에서 기존 SITL/관제 명령을 다시 사용한다. 공장·창고의 외장 패널/지붕/
로딩도어, 펜스, 물류 적재물, 차선과 3m 반복 UV 콘크리트 바닥을 추가했다.
건물/장애물은 정적이며 collision은 단순 box, 펜스는 panel당 하나의 메시다.
구조물은 직접 만든 형상이며 실측 공장 스캔 모델이라고 주장하지 않는다.

```sh
python tools/gazebo/industrial/measure_scene.py capture site
```

### 3. 화염·연기 효과 추가

site 단계까지 확인한 뒤 Gazebo/SITL을 종료하고:

```sh
zsh tools/gazebo/run_industrial_world.sh effects
```

화염과 연기는 기존 색칠한 원기둥 대신 알파 마스크 billboard 입자를 사용한다.
각각 24개/s×1.5초, 12개/s×8초로 예상 최대 동시 입자는 36+96개다.
파티클 방향은 +Z가 되도록 모델 pitch를 설정했다. 동적 점광원은 추가하지 않았다.
SDF 파싱은 확인했지만 Metal에서의 투명도/색상/카메라 렌더링은 미확인이다.
효과는 시각적 연출이며 CFD·온도·연소 물리 또는 실제 화재 영상이 아니다.

```sh
python tools/gazebo/industrial/measure_scene.py capture effects
python tools/gazebo/industrial/measure_scene.py compare
```

수신 목표 범위는 9–11FPS로 별도 표시한다. `capture.verified=true`는 프레임 수신
성공이며 목표 FPS 충족을 뜻하지 않는다. baseline이 성공해야 비교표를 생성한다.
10Hz 센서의 상한 때문에 FPS만 같아도 추가 GPU 부담이 없다고 결론 내리지 않는다.
동일 pose/조명/카메라/UI 부하에서 RTF와 간격도 비교한다. 이번 세션의 실제 수치는 미측정이다.

### 4. YOLO 별도 검사

먼저 동일 시점 캡처를 확보한 뒤 기존 관제 Python 환경에서:

```sh
python tools/gazebo/industrial/measure_scene.py detect worker --device mps
python tools/gazebo/industrial/measure_scene.py detect effects --device mps
```

기존 로컬 weights만 사용한다: yolo11s.pt(person, conf .35, imgsz 832),
fire_smoke_best.pt(conf .30, imgsz 640), ppe_best.pt(conf .40, imgsz 640).
결과는 각 stage의 `detections/results.json`과 박스 표시 JPG다.
`inference_completed`와 모델별 `frames_with_detections`를 구분하며 탐지 0건을 성공으로 표시하지 않는다.
이 검사는 실제 Gazebo 카메라에서 받은 시뮬레이션 영상을 대상으로 한다.
실제 촬영 사람·화재 데이터에서의 정확도나 기존 관제의 시간 누적/맥락 필터/이벤트 경고
발생까지 검증하는 도구는 아니다. 정답 라벨이 없으므로 mAP·정확도를 산출하지 않는다.
실제 관제 경고는 기존 UI에서 별도로 확인해야 한다.

## 검증 기록

- 기존 파일 SHA256 보존 검사 통과.
- Python/zsh 구문 검사 통과.
- 설치된 sdformat CLI로 세 월드 `gz sdf -k` **Valid, exit 0** 확인.
  CLI에는 `GZ_SIM_RESOURCE_PATH`와 별개로 `SDF_PATH`를 지정했다.
  upstream 센서의 gz_frame_id 확장 경고는 남는다. 메시/텍스처 렌더 테스트는 아니다.
- 오프라인 테스트 7개 통과: 기체/물리/좌표계 보존, 한 명·단계 분리, 에셋 타입/해시
  거부, 실패한 캡처가 이전 성공을 덮는 처리, 측정 완료와 목표 FPS 판정 구분.
- 에셋 다운로드: DNS 오류로 실패. 따라서 작업자/콘크리트 바이너리 **미설치**.
- worker launcher: 에셋 누락을 확인하고 **서버 실행 전에 종료**. 실제 worker 렌더 미검증.
- baseline 수신 시도: X500 토픽 미발견으로 실패. 사용자 환경의 기존 연결 성공을
  부정하는 결과가 아니며, 이번 도구 세션에서는 프레임을 확인하지 못했다.
- baseline/worker/site/effects의 FPS 비교, 신규 환경 렌더, YOLO 추론은 **미검증**.

근거: `diagnostics/gazebo/industrial/{assets-attempt,launch-attempt,baseline-attempt,sdf-check,offline-tests}.log`.
성공 여부를 채운 가짜 비교표나 탐지 결과는 만들지 않았다.

## 재생성 / 원상 복귀

새 월드를 재생성하려면:

```sh
python tools/gazebo/industrial/build_worlds.py
"$GAZEBO_PYTHON" tools/gazebo/industrial/test_industrial.py
```

새 월드와 SITL을 정상 종료한 뒤 기존 명령으로 원본 환경을 실행하면 된다:

```sh
zsh tools/gazebo/run_x500_camera.sh
zsh tools/gazebo/run_x500_sitl.sh
```

원본 파일 복원이나 렌더러 설정 재변경은 필요하지 않다.
