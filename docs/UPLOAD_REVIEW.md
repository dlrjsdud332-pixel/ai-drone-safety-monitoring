# 개발 프로토타입 v0.1 업로드 검토

2026-10-20 현재 기존 GitHub 이력을 보존한 clone에 후보 반영과 문서 정리를 진행했다. 최종 공개 승인과 배포 권리 검토는 미완료다. stage·commit·push·원격 브랜치 생성·이력 재작성·기존 파일 삭제는 실행하지 않았다.

## Git 연결과 이력

- 현재 작업 폴더는 `.git`과 origin이 있는 GitHub clone이다. 원본 개발 폴더는 읽기 전용으로 취급한다.
- 현재 로컬 브랜치: **`release/prototype-v0.1`**.
- 기준 HEAD: `779ea4a5c2a55326709a65a389f730f0cd2dfc10` (`feat: build autonomous drone agent monitoring pipeline`). 원격 최신 상태나 전체 역사 감사를 완료했다는 뜻은 아니다.
- origin: `https://github.com/dlrjsdud332-pixel/ai-drone-safety-monitoring.git`.
- 원본 준비 당시 터미널 clone/DNS 실패와 읽기 API 비교 기록은 과거 준비 단계다. 현재는 실제 clone에서 `git status`와 `git diff`를 확인했다.

## 보존과 제외

공개 후보는 `.gitignore`로 필터링하고 토큰·개인 키·개인 절대경로·이메일·대용량·촬영 바이너리·Python 구문을 검사한다. 준비 후보와 실제 반영 범위는 [UPLOAD_FILES.md](UPLOAD_FILES.md)에 기록한다. 원본 개발 폴더의 `diagnostics/prototype_v0_1/bundle/manifest.json`은 준비 당시 해시이며 후속 문서 수정 전 기준이다. clone의 반영·최종 검사 증거는 Git 제외 `diagnostics/github_v0_1_transfer/`, `diagnostics/github_v0_1_final/`, `diagnostics/github_v0_1_docs/`에 기록한다. 이 검사는 패턴 기반으로 개인정보 부재를 보증하지는 않는다.

핵심 `legacy/drone_tracker.py`, 루트 Python 모듈, `tools/gazebo` worker/어댑터/전방 카메라 SDF 모델/기존 실행 스크립트, AI 검증 도구, 문서와 의존성 파일을 포함한다. 기존 `reports/*_report.py` 3개는 **소스**이므로 예외적으로 포함하고 생성 보고서는 제외한다. PX4 본체는 외부 설치이며 복사하지 않는다.

제외: 영상·프레임·이벤트·GPS·검수 JSON·데이터셋·모델 가중치·다운로드 외부 3D/텍스처·가상환경·캐시·개인 에디터 설정·임시 로그·백업. 원본 파일은 모두 로컬에 보존한다. 모델이 제외되어 소스만으로 AI가 즉시 실행되지는 않으며 README의 모델별 별도 설치와 권한 확인이 필요하다.

**원격에 이미 있는 파일:** 이벤트/로그/영상/스냅샷 18개와 가중치 4개가 clone 기준 HEAD와 현재 작업 브랜치에서 추적 중이다. `.gitignore` 또는 새 overlay에서 제외해도 clone에는 그대로 남는다. 추가 공개를 피하려면 최종 승인 후 새 브랜치에서 해당 파일의 추적 해제를 별도 검토해야 한다. `git rm --cached`는 로컬 파일을 보존하지만 이후 커밋에서 제거 변경이 되므로 이번에는 실행하지 않았다. 최신 브랜치에서 제거해도 과거 이력에는 남으며, 이력을 보존하면서 과거 공개가 사라졌다고 주장하지 않는다. 기존 원격에서 가져온 `legacy/drone_tracker_v3_event_folders.py`도 보존 대상으로 분리했다.

## 현재 반영 결과

- 준비 후보 99개 중 98개 반영: HEAD 대비 추가 **84개**, 수정 **4개**, 동일 후보 **10개**. 삭제 0개.
- 수정 4개: `.gitignore`, `README.md`, `legacy/drone_tracker.py`, `requirements.txt`. 운영 코드 변경분은 준비된 후보 반영 결과이며 이번 후속 문서 정리에서 운영 코드·AI·PX4·Gazebo 설정을 추가 변경하지 않았다.
- 보류 1개: 루트 `drone_tracker.py` 후보는 0바이트이므로 반영하지 않았다. 기존 독립 실행 코드는 HEAD와 동일하게 보존한다.
- 주 관제 프로그램은 **`legacy/drone_tracker.py`**다. 교육용 시뮬레이터는 `legacy/drone_simulator_app.py`이며 루트 tracker와 혼용하지 않는다.
- 원격 전용/제외 23개(데이터·가중치 22개와 기존 소스 1개)는 보존했다.
- 후속 README·업로드 안내 문서 수정은 기존 후보 안에서 진행했으므로 추가/수정 파일 수는 그대로다. 준비 당시 SHA256·약 1.02MiB 집계는 현재 문서 바이트의 검증값이 아니다.

## 검증과 미완료

기존 오프라인 검사 67건: **65통과·1실패·1오류**. 산업현장 잔여 2건과 GUI/비행·새 환경 설치·정식 정확도·전체 배포 라이선스 미완료는 [VALIDATION_REPORT.md](VALIDATION_REPORT.md)에 유지한다. 후속 문서 정리에서 성능 실험·단위 테스트를 재실행하지 않았다.

후보 준비 당시 Python 57개 구문·shell/XML/필수 소스 18건 검사가 통과했다. clone 검사에서 `git diff --check` 통과와 빈 스테이징 영역을 확인했다. 후보의 패턴 검사 발견 0건은 기존 추적 데이터나 Git 역사까지 개인정보가 없다는 뜻이 아니다. 기존 이벤트 JSON 5개에는 개인 절대경로가 있으며 위치 필드는 null이다. 권리·개인정보 검토가 끝나기 전 공개 준비 완료로 표시하지 않는다.

## 남은 절차

기존 clone과 로컬 작업 브랜치를 사용하며 clone·브랜치 생성·overlay 전체 재복사를 반복하지 않는다. 특히 0바이트 루트 후보는 복사하지 않는다. 변경 파일 목록과 이미 추적된 모델·이벤트의 권리 및 개인정보를 검토한다. 향후 별도 지시 전에는 stage·commit·push하지 않는다. force push나 이력 재작성은 사용하지 않는다.

커밋 메시지 제안: **`chore: prepare development prototype v0.1`**.
