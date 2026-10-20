# v0.1 공개 소스 후보 파일

원본 준비 후보 99개를 아래에 보존한다. 현재 clone에서는 98개를 반영했으며 HEAD 대비 추가 84개·수정 4개·동일 후보 10개다. 루트 `drone_tracker.py`의 0바이트 후보 1개는 보류하고 기존 독립 실행 코드를 유지한다. 따라서 실제 변경 목록은 88개이며 아래 99개 전부를 stage할 목록으로 사용하지 않는다. 후속 문서 수정도 기존 후보 안에 포함된다. 모델·비공개 데이터·진단 출력은 새 후보에 포함하지 않으며 기존 추적 파일은 별도로 보존한다. stage·commit·push는 실행하지 않았다.

## 원본 준비 후보 목록 (루트 tracker 후보는 보류)

- `.gitignore`
- `README.md`
- `README_ko.md`
- `ai_tracker.py`
- `ai_worker.py`
- `docs/LICENSE_REVIEW.md`
- `docs/THIRD_PARTY_NOTICES.md`
- `docs/UPLOAD_FILES.md`
- `docs/UPLOAD_REVIEW.md`
- `docs/VALIDATION_REPORT.md`
- `docs/third_party/PX4-gazebo-models-BSD-3-Clause.txt`
- `drone_tracker.py`
- `drone_udp_simulator.py`
- `fall_detector.py`
- `legacy/drone_simulator_app.py`
- `legacy/drone_tracker.py`
- `legacy/drone_tracker_px4_medium.py`
- `main.py`
- `models/README.md`
- `ppe_detector.py`
- `px4_telemetry_test.py`
- `reports/danger_report.py`
- `reports/fall_report.py`
- `reports/ppe_report.py`
- `requirements-macos-tested.txt`
- `requirements.txt`
- `stream_capture.py`
- `stream_resolver.py`
- `test_agent_safety.py`
- `test_yolo26_compare.py`
- `tools/ai/check_case_scale.py`
- `tools/ai/check_fire_review_js.py`
- `tools/ai/compare_crowd_detection.py`
- `tools/ai/compare_crowd_regions.py`
- `tools/ai/compare_detection.py`
- `tools/ai/compare_fire_preprocessing.py`
- `tools/ai/compare_fire_preprocessing_effects.py`
- `tools/ai/compare_hsv_roi.py`
- `tools/ai/crowd_regions.py`
- `tools/ai/evaluate_crowd_detection.py`
- `tools/ai/fire_review.html`
- `tools/ai/inspect_crowd_targets.py`
- `tools/ai/prepare_fire_dataset.py`
- `tools/ai/prepare_fire_review.py`
- `tools/ai/replay_tracker.py`
- `tools/ai/save_review_cases.py`
- `tools/ai/test_alarm_logic.py`
- `tools/ai/test_crowd_detection_evaluation.py`
- `tools/ai/test_crowd_logic.py`
- `tools/ai/test_crowd_regions.py`
- `tools/ai/test_fire_dataset.py`
- `tools/ai/test_fire_review.py`
- `tools/ai/test_hsv_roi.py`
- `tools/ai/validate_crowd.py`
- `tools/ai/validate_crowd_video.py`
- `tools/ai/validate_gui.py`
- `tools/gazebo/README.md`
- `tools/gazebo/SENSOR_SYNC.md`
- `tools/gazebo/TRACKER_INTEGRATION.md`
- `tools/gazebo/X500_CAMERA.md`
- `tools/gazebo/camera_probe.sdf`
- `tools/gazebo/camera_source.py`
- `tools/gazebo/check_x500_sync.py`
- `tools/gazebo/industrial/ASSET_REVIEW.md`
- `tools/gazebo/industrial/LICENSE.generated-assets.txt`
- `tools/gazebo/industrial/README.md`
- `tools/gazebo/industrial/assets.json`
- `tools/gazebo/industrial/assets.lock.json`
- `tools/gazebo/industrial/build_worlds.py`
- `tools/gazebo/industrial/industrial_effects.sdf`
- `tools/gazebo/industrial/industrial_site.sdf`
- `tools/gazebo/industrial/measure_scene.py`
- `tools/gazebo/industrial/models/industrial_materials/meshes/fence.obj`
- `tools/gazebo/industrial/models/industrial_materials/meshes/ground.obj`
- `tools/gazebo/industrial/models/industrial_materials/model.config`
- `tools/gazebo/industrial/models/industrial_materials/model.sdf`
- `tools/gazebo/industrial/models/industrial_worker/model.config`
- `tools/gazebo/industrial/models/industrial_worker/model.sdf`
- `tools/gazebo/industrial/prepare_assets.py`
- `tools/gazebo/industrial/test_industrial.py`
- `tools/gazebo/industrial/worker_only.sdf`
- `tools/gazebo/models/CAMERA_CLEARANCE.md`
- `tools/gazebo/models/mono_cam_clear/model.config`
- `tools/gazebo/models/mono_cam_clear/model.sdf`
- `tools/gazebo/models/x500_mono_cam_clear/model.config`
- `tools/gazebo/models/x500_mono_cam_clear/model.sdf`
- `tools/gazebo/receive_probe.py`
- `tools/gazebo/run_camera_probe.sh`
- `tools/gazebo/run_industrial_world.sh`
- `tools/gazebo/run_x500_camera.sh`
- `tools/gazebo/run_x500_sitl.sh`
- `tools/gazebo/test_camera_source.py`
- `tools/gazebo/test_verify_x500_camera.py`
- `tools/gazebo/test_x500_sync.py`
- `tools/gazebo/verify_x500_camera.py`
- `tools/gazebo/x500_camera.sdf`
- `tools/release/audit_upload.py`
- `tools/release/check_prototype.py`
- `tools/release/prepare_prototype.py`

## 제외 정책

- `videos/`, `data/`, `diagnostics/`와 생성 `reports/*`: 영상·사진·GPS·이벤트·검수 JSON·백업·로그.
- 모델 `.pt`/`.onnx`/`.engine`: 원 가중치와 사용자 정의 가중치 모두 별도 설치.
- 외부 다운로드 작업자 GLB·콘크리트 텍스처, 환경·캐시·개인 설정·키 파일·대용량 영상.
- `reports/danger_report.py`, `reports/fall_report.py`, `reports/ppe_report.py`는 기존 소스이므로 포함 예외.

## 원격에서 이미 추적 중인 제외 대상

아래22개는 새 묶음에서 제외했지만 기존 원격에는 남아 있다. 이번에 삭제/추적 해제/이력 변경하지 않았다. 최종 공개 결정이 필요하다.

- `data/events/camera_2/CROWD/20261012_172811_644/event.json`
- `data/events/camera_2/CROWD/20261012_172811_644/event.mp4`
- `data/events/camera_2/CROWD/20261012_172811_644/snapshot.jpg`
- `data/events/camera_2/NO_HARDHAT/20261012_172822_108/event.json`
- `data/events/camera_2/NO_HARDHAT/20261012_172822_108/snapshot.jpg`
- `data/events/camera_2/NO_VEST/20261012_172822_120/event.json`
- `data/events/camera_2/NO_VEST/20261012_172822_120/snapshot.jpg`
- `data/events/camera_2/NO_VEST/20261012_172822_150/event.json`
- `data/events/camera_2/NO_VEST/20261012_172822_150/snapshot.jpg`
- `data/events/camera_2/NO_VEST/20261012_172822_172/event.json`
- `data/events/camera_2/NO_VEST/20261012_172822_172/snapshot.jpg`
- `data/old_logs/danger_events.csv`
- `data/old_logs/fall_events.csv`
- `data/old_logs/multi_tracker/camera_1/danger_events.csv`
- `data/old_logs/multi_tracker/camera_2/ppe_events.csv`
- `data/old_logs/multi_tracker/camera_3/ppe_events.csv`
- `data/old_logs/ppe_events.csv`
- `data/old_logs/ui_zone_events.csv`
- `models/ppe_best.pt`
- `models/yolo11n-pose.pt`
- `models/yolo11n.pt`
- `models/yolo11s.pt`

원격 전용 `legacy/drone_tracker_v3_event_folders.py`는 기존 파일 보존 대상으로 둔다. 로컬에서 없다는 이유로 삭제하지 않는다. 원본 개발 폴더의 bundle/manifest.json은 준비 당시 비교 자료다. 현재 문서 수정 후 상태는 clone의 `git status`, `git diff`와 Git 제외 `diagnostics/github_v0_1_docs/` 검사 기록을 따른다.
