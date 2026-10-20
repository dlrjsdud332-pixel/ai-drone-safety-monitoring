# 구현 전 에셋 검토 — 2026-10-19

| 후보 / 원본 | 배포자 명시 라이선스 | 형식 / 부담 예상 | 결정 |
|---|---|---|---|
| [Innerscene Drew construction worker](https://www.innerscene.com/tools/library/3d-parts/drew-3d-person-construction-worker-with-hard-hat-and-hi-vis-a4aa98af) | CC0; MakeHuman/MPFB CC0 몸체·피부 기반이라고 명시 | GLB 22.8 MB, 실척 1.888m 높이, 텍스처 포함. 삼각형/텍스처 해상도는 파일 확보 후 측정 | 한 명 정적 모델로 우선 선택. 안전모/hi-vis 작업복 포함. 다운로드/육안 검증 대기 |
| [Quaternius Worker](https://poly.pizza/m/Yg2bQZO6Hj), [제작자 팩](https://quaternius.com/packs/ultimatedanimatedcharacter) | CC0 | FBX/OBJ/Blend/glTF 배포, 게임풍 저폴리로 상대적 GPU 부담이 작을 것으로 예상 | 기존 저품질 외형 개선 목표와 맞지 않아 미사용 |
| [MakeHuman 자체 제작](https://github.com/makehumancommunity/makehuman/blob/master/LICENSE.md) | 기본 번들/출력 CC0; 추가 의상은 개별 라이선스 | OBJ/DAE/FBX 출력; 몸체·작업복 제작과 텍스처 정리가 추가로 필요 | 추후 대안. 커뮤니티 의상을 일괄 CC0로 간주하지 않음 |
| [Poly Haven Concrete Floor Worn 001](https://polyhaven.com/a/concrete_floor_worn_001) | CC0, [공식 조건](https://polyhaven.com/license) | diffuse/roughness/OpenGL normal 1K JPG부터 시작. RGBA로 로딩되면 맵당 약 4MiB, mipmap 포함 약 5.3MiB의 추정치 | site 단계에 적용; 4K/16K·displacement 미사용 |

선택 작업자의 [라이브러리 조건](https://www.innerscene.com/tools/library/terms)도 확인했다.
개별 파일의 CC0 표시와 출처를 manifest에 보존한다. 로그인/유료/NC/라이선스 불명 모델을
대체 에셋으로 자동 선택하지 않는다. 실제 파일을 확보하지 못한 현재에는 원본 바이너리의
내용·삼각형 수·작업복 외형을 검증했다고 주장할 수 없다.

## Gazebo Sim 8 / Apple Silicon 형식

[Sim 8 mesh 문서](https://gazebosim.org/api/sim/8/meshtofuel.html)는 DAE/OBJ/STL을 설명한다.
[gz-common5 MeshManager](https://github.com/gazebosim/gz-common/blob/gz-common5/graphics/src/MeshManager.cc)
에는 glTF/GLB/FBX를 AssimpLoader로 전달하는 경로도 있다. 설치 버전은 gz-common 5.7.1,
Assimp 6.0.5이며 binary 문자열에서 GLTF와 embedded texture 처리를 확인했다.
따라서 우선 GLB 원본을 사용하고, 실제 재질·방향·스케일은 one-worker 렌더에서 확인한다.
지원 확장자 확인은 Metal 렌더 성공 증거가 아니다. Draco/meshopt/KTX2만 들어 있는 모델은
현재 설치 도구가 거부한다. skeletal animation은 이번 단계에서 사용하지 않는다.

GLB 재질 호환 문제가 실제로 발생한 경우에만 Blender 등에서 정적 pose를 bake하고,
메시 변환/triangulate/미터 단위/Z-up 적용 후 OBJ+MTL+PNG 또는 DAE+PNG로 내보낸다.
OBJ는 PBR 정보 손실 가능성이 있고 DAE도 PBR 그대로 보존을 보장하지 않는다.
이번 환경에 Blender가 없으며 포맷 변환은 실행하지 않았다. 렌더러/Metal 옵션은 변경하지 않는다.

## 성능 예산과 단계

- worker: 기존 X500/물리/카메라 + 정적 작업자 한 명만. 블록 사람과 가짜 화염은 제거.
- site: worker 단계가 정상인 것을 확인한 뒤 콘크리트/공장/창고/안전 펜스 추가.
  건물은 세부 패널과 프레임을 쓰되 정적이며 collision은 간단한 box로 제한.
- effects: site 단계 후 smoke와 flame particle emitters 추가. rate×lifetime 기준
  예상 동시 입자는 smoke 96개 + flame 36개. 투명 입자 overdraw가 GPU 부담을 늘릴 수 있다.
  [Sim 8 particle emitter](https://gazebosim.org/api/sim/8/particle_emitter.html)는 RGB 카메라 표시를 지원한다.
  이는 화재 물리/연기 CFD/탐지 성능 검증과 다르다.
- RGB 해상도/센서 10Hz/OGRE2 Metal/모델명/월드명/비행 설정을 유지한다.
  동일 기체 위치·UI/추론 부하·수신 환경에서 baseline→worker→site→effects 순서로 측정한다.
- 640×480 BGR 프레임은 약 0.88MiB, 10Hz 원시 데이터는 약 8.79MiB/s다.
  실시간 FPS·RTF·YOLO latency는 측정 전 숫자를 채우지 않는다.

외부 다운로드는 이 세션의 DNS 제한으로 실패했다. 설치/렌더/탐지 상태는 README와
`diagnostics/gazebo/industrial/`에 별도로 기록하며 후보 검토와 혼동하지 않는다.
