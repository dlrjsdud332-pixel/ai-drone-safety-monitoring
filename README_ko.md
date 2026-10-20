# Drone Lab: 드론 시뮬레이터 (첫 버전)

이 앱은 **실제 드론에 연결하지 않는** 로컬 2D 교육용 시뮬레이터입니다.

## 설치 및 실행

- macOS 내 Python 3가 필요하며 Tkinter가 포함되어 있어야 합니다.
- 추가 pip 패키지가 필요하지 않습니다.
- `python3 legacy/drone_simulator_app.py` 로 실행합니다.
- 화면에서 드론 선택 후 `TAKEOFF + WAYPOINT MISSION`, `RETURN TO HOME`, `LAND HERE`, `RESET SELECTED`를 사용합니다.

## 기존 관제 프로그램 연동

주 관제 프로그램은 `legacy/drone_tracker.py`입니다. 루트 `drone_tracker.py`는 기존 독립 실행 코드로 보존하며 아래 UDP 연동 명령과 혼용하지 않습니다. 모든 명령은 프로젝트 루트에서 실행합니다.

1. 기존 `legacy/drone_tracker.py`를 별도로 백업합니다. 원본 코드는 수정하지 않습니다.
2. 기존 `drone_udp_simulator.py`가 실행 중이라면 중지합니다. 동일 포트를 중복 사용할 수 있습니다.
3. 첫 번째 터미널에서 이 앱을 실행하고 `Connect UDP Bridge` 버튼을 누릅니다.
4. 두 번째 터미널에서 기존 프로젝트 루트로 이동하여 아래 명령을 실행합니다.

   `python3 legacy/drone_tracker.py --flight-adapter UDP <본인이_사용하는_영상_파일_경로>`

   영상 파일 경로를 생략하면 `legacy/drone_tracker.py`의 기본 영상 소스가 열리므로, 본인이 검증한 영상 경로를 지정하는 것이 안전합니다.
5. `SIM-01`의 상태를 수신하는지 관제 화면에서 확인합니다. 연결 모드는 PX4가 아닌 UDP입니다.

## 통신 인터페이스

- 시뮬레이터 → 관제: `127.0.0.1:14560` UDP JSON
- 관제 → 시뮬레이터: `127.0.0.1:14561` UDP JSON
- 기본 프로토콜은 기존 `TelemetryInputAdapter.ingest()` / `UdpFlightAdapter.send()`와 정렬했습니다.
- 전송 필드: `drone_id`, `battery`, `altitude_m`, `heading_deg`, `flight_mode`, `armed`, `last_command`, `last_command_id`, `simulation`.
- 가짜 GPS 값을 생성하지 않습니다. PX4 미션 프로토콜이나 MAVLink를 구현하지 않습니다.
- 관제 측 명령 `RTH`, `LAND`는 시뮬레이터 내에서만 실행합니다. `PATROL`, `TRACK`, `ORBIT`는 자동 비행하지 않고 기록만 합니다.
- 다중 기체는 시뮬레이터 내부에만 분리되어 있으며 기존 관제로의 전송은 `SIM-01`만 지원합니다.

## 안전 및 제약

실제 드론 펌웨어, PX4 SITL 미션 프로그레스, DJI, CoDrone EDU 기체 제어가 아닙니다. 앱은 로컬 루프백 주소로만 통신합니다. UDP는 신뢰성 보장이나 명령 ACK를 제공하지 않으며, 별도 비행 제어 장치의 안전 기능을 대체하지 않습니다.

## 연동 검증

`python3 -m py_compile legacy/drone_simulator_app.py`

별도 창이 열리지 않는 경우 현재 Python 설치에 Tk가 포함되어 있는지 `python3 -m tkinter`로 확인하세요.
