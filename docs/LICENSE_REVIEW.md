# 배포 전 라이선스 검토

2026-10-20 확인. 프로젝트 전체에 새 라이선스를 임의로 부여하지 않았다. 최종 배포 라이선스 결정은 미완료다.

- Ultralytics는 코드 및 모델에 AGPL-3.0/Enterprise 조건을 안내한다. 소스 공개만으로 모든 조건 충족이 자동 보장되는 것은 아니다. [공식 조건](https://www.ultralytics.com/license)을 기준으로 프로젝트 배포 조건을 확인해야 한다.
- `ppe_best.pt`, `fire_smoke_best.pt`: 원 배포처·학습 데이터·재배포 허가 근거가 로컬에 없어 확인 미완료. 파일은 보존하되 모든 `.pt`/`.onnx`/`.engine`을 업로드 후보에서 제외했다. 공식 YOLO 가중치도 이번 소스 업로드에 넣지 않는다.
- 로컬 촬영/다운로드 영상, 이미지와 사용자 검수 JSON: 출처/동의/배포 권한 미확인. `videos/`, `diagnostics/`, `data/`, `reports/`를 제외한다. 사용자 이름과 이벤트 GPS가 들어갈 수 있는 원본 로그도 제외한다.
- Innerscene Drew 작업자 모델은 [모델 페이지](https://www.innerscene.com/tools/library/3d-parts/drew-3d-person-construction-worker-with-hard-hat-and-hi-vis-a4aa98af)와 [라이브러리 조건](https://www.innerscene.com/tools/library/terms)에서 CC0 표시를 확인했다. 로컬 다운로드 GLB는 대용량이므로 기본 업로드에서 제외하고 준비 스크립트/출처/해시만 보존한다. 렌더 성공을 의미하지 않는다.
- Poly Haven은 [자산 라이선스](https://polyhaven.com/license)에서 자산을 CC0로 안내한다. 외부 다운로드 텍스처도 기본 업로드에서 제외한다. 사이트 코드/로고/웹페이지 콘텐츠까지 CC0로 간주하지 않는다.
- 프로젝트 생성 산업현장 메시/효과에는 기존 `tools/gazebo/industrial/LICENSE.generated-assets.txt`를 유지했다. PX4/Gazebo 원본은 저장소에 복사하지 않았고 별도 설치가 필요하다.

가중치·원본 영상·제3자 바이너리를 제외해도 사용하는 라이브러리와 프로젝트 전체의 배포 의무 검토가 끝난 것은 아니다. 현재 상태를 “라이선스 검증 완료”로 표시하지 않는다.

## v0.1 공개 범위 결정

공식 모델 페이지와 이용 조건을 이번 준비에서 다시 열어 Drew 및 Poly Haven 자산의 CC0 표기를 확인했다. CC0 자산이라도 이번에는 외부 GLB·텍스처 바이너리를 업로드하지 않고 출처·준비 스크립트만 포함한다. 로컬 테스트 영상·화재 데이터셋·검수 JSON은 출처 및 개인정보/재배포 허가가 미확인이라 제외한다. 별도 데이터셋 라이선스를 추정해 부여하지 않는다.

원격 `main`에는 기존 YOLO/PPE 가중치 4개가 이미 추적되어 있다. 새 소스 묶음에서 제외하는 것과 기존 공개 이력에서 제거하는 것은 다르다. 이력 재작성이나 강제 push는 하지 않았으며, 최종 승인 전에 최신 브랜치에서의 추적 해제 여부와 배포 라이선스를 결정해야 한다. 커스텀 화재·PPE 가중치 권한과 프로젝트 전체 LICENSE는 미완료다. 라이선스 문서나 별도 승인 없이 상업적 사용 가능 프로젝트로 표시하지 않는다.

로컬 카메라 SDF 파생본의 원본 PX4-gazebo-models BSD-3-Clause 고지와 출처를 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 추가했다. 기존 센서/물리 설정은 변경하지 않았다.
