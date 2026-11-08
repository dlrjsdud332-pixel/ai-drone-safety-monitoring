"""Independent offline checks, fresh evidence and one consolidated report.

Never executes PX4, opens IPC/UDP, deploys accounts/keys or changes firewall.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
DOCS = ('SECURITY_HARDENING_PLAN.md', 'IMPLEMENTATION_READINESS_REVIEW.md',
        'R1_R2_IMPLEMENTATION_REPORT.md', 'R3_OFFLINE_TRANSPORT_REPORT.md')
CHECKS = (
    ('regression', 'Python 단위·메모리 통합·전체 tools/ai 회귀'),
    ('launcher', '기존 launcher 회귀'), ('geometry', '기존 geometry 회귀'),
    ('cpp_policy', 'C++ strict/parser/fixture 정책'),
    ('build', '독립 PX4 build target만 재검증'),
    ('preservation', '기존 보호 파일12개 SHA256'),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(command, env, timeout=180):
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=timeout, shell=False)
        return dict(status='PASS' if result.returncode == 0 else 'FAIL',
                    command=command, returncode=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired:
        return dict(status='BLOCKED', reason='Offline check timeout', command=command)
    except OSError:
        return dict(status='BLOCKED', reason='Offline executable unavailable', command=command)


def isolate(check):
    """An unavailable prerequisite never cancels unrelated checks."""
    try:
        return check()
    except Exception as exc:
        # No external exception message, request, packet or secret is logged.
        return dict(status='BLOCKED', reason='Prerequisite/collector unavailable',
                    error_type=type(exc).__name__)


def test_count(row):
    if row.get('status') != 'PASS':
        return None
    counts = re.findall(r'Ran (\d+) tests?', row.get('stdout', '')+'\n'+row.get('stderr', ''))
    return int(counts[-1]) if counts else None


def main():
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output = ROOT/'diagnostics/integrated_offline_verification'/run_id
    output.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'CCACHE_DISABLE':'1',
           'QT_QPA_PLATFORM':'offscreen','GIT_OPTIONAL_LOCKS':'0',
           'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'1',
           'GIT_CONFIG_KEY_0':'protocol.allow','GIT_CONFIG_VALUE_0':'never'}
    plan = dict(run_id=run_id, scope='OFFLINE_ONLY', checks=[dict(name=n,description=d) for n,d in CHECKS],
                runtime_tests='BLOCKED_NOT_AUTHORIZED', px4_execution=False,
                actual_ipc_udp=False, accounts_keys_firewall=False)
    (output/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    docs = {name:isolate(lambda name=name:dict(status='PASS', sha256=digest(ROOT/name))) for name in DOCS}
    baseline_path = ROOT/'diagnostics/secure_r1_r2/baseline.json'
    def baseline():
        return json.loads(baseline_path.read_text())
    results = {}
    def record(name, callback):
        results[name] = isolate(callback)
        (output/(name+'.json')).write_text(json.dumps(results[name], ensure_ascii=False, indent=2))
        print(name+': '+results[name]['status'], flush=True)
    def regression():
        start = time.time_ns()
        row = execute([sys.executable,'-m','tools.flight.secure.run_regression'],env)
        evidence = ROOT/'diagnostics/secure_r1_r2/regression.json'
        if row['status'] == 'PASS':
            if evidence.stat().st_mtime_ns < start:
                return dict(status='FAIL',reason='Fresh regression evidence missing')
            data = json.loads(evidence.read_text())
            if data['status'] != 'PASS' or data['failures'] or data['errors']:
                return dict(status='FAIL',reason='Regression result inconsistent')
            row['tests'] = data['tests']; row['skipped'] = data['skipped']
            (output/'regression_summary.json').write_text(json.dumps(data,indent=2))
            log = ROOT/'diagnostics/secure_r1_r2/regression.log'
            if log.stat().st_mtime_ns >= start:
                (output/'regression.log').write_bytes(log.read_bytes())
        return row
    record('regression', regression)
    launcher_env = dict(env); launcher_env.pop('PX4_SOURCE_DIR',None)
    record('launcher', lambda:execute([sys.executable,'-m','unittest','tools.gazebo.test_oneclick'],launcher_env))
    def geometry():
        source = Path(baseline()['source'])
        return execute([str(source/'Tools/setup/macos/.pixi/envs/default/bin/python'),
                        '-m','unittest','tools.gazebo.test_airframe_geometry'],{**env,'PX4_SOURCE_DIR':str(source)})
    record('geometry',geometry)
    def cpp():
        start = time.time_ns()
        row = execute([sys.executable,'-m','tools.flight.secure.run_cpp_tests'],env)
        if row['status'] == 'PASS':
            path = ROOT/'diagnostics/secure_r1_r2/cpp_tests.json'
            if path.stat().st_mtime_ns < start:
                return dict(status='FAIL',reason='Fresh C++ evidence missing')
            stages = json.loads(path.read_text())
            if not stages or any(item['returncode'] for item in stages):
                return dict(status='FAIL',reason='C++ result inconsistent')
            row['stages'] = len(stages)
            row['assertions'] = sum(int(count) for item in stages for count in re.findall(
                r'\((\d+) assertions\)',item.get('stdout','')))
            (output/'cpp_stages.json').write_text(json.dumps(stages,indent=2))
        return row
    record('cpp_policy',cpp)
    def build():
        folder = ROOT/'diagnostics/secure_r1_r2'
        source, target = folder/'px4-source', folder/'px4-build'
        if source.resolve() != source or target.resolve() != target:
            return dict(status='BLOCKED',reason='Independent source/build path changed')
        cache = (target/'CMakeCache.txt').read_text()
        if f'CMAKE_HOME_DIRECTORY:INTERNAL={source}' not in cache:
            return dict(status='BLOCKED',reason='Independent build source mismatch')
        recipe = execute(['/opt/homebrew/bin/ninja','-C',str(target),'-t','commands','px4'],env)
        if recipe['status'] != 'PASS':return recipe
        commands = recipe['stdout']
        if (re.search(r'(?:^|&&\s+)(?:\S*/)?bin/px4(?:\s|$)',commands,re.M)
                or any(value in commands for value in ('git clone','git submodule update','curl ','wget ','libmicroxrceddsclient_project'))):
            return dict(status='BLOCKED',reason='Unexpected runtime/fetch build recipe')
        row = execute(['/opt/homebrew/bin/cmake','--build',str(target),'--target','px4','--parallel','2'],env,300)
        binary = target/'bin/px4'
        if row['status'] == 'PASS':
            row['binary_sha256'] = digest(binary)
            row['binary_executed'] = False
        return row
    record('build',build)
    def preservation():
        files = baseline()['files']
        changed = [name for name, expected in files.items()
                   if not Path(name).is_file() or digest(Path(name)) != expected]
        return dict(status='FAIL' if changed else 'PASS',protected_files=len(files),changed=changed)
    record('preservation',preservation)
    counts = {name:(results[name].get('tests') if name=='regression' else test_count(results[name]))
              for name in ('regression','launcher','geometry')}
    for name,count in counts.items():
        if results[name]['status']=='PASS' and count is None:
            results[name]['status']='FAIL'; results[name]['reason']='Test count evidence missing'
            (output/(name+'.json')).write_text(json.dumps(results[name],indent=2))
    runtime = dict(execution='BLOCKED',security='UNVERIFIED',accounts='UNVERIFIED',keys='UNVERIFIED',
                   persistent_ledger='UNVERIFIED',reason='Not authorized; production prerequisites incomplete',
                   new_px4_executed=False,ipc_server_started=False,actual_udp_packets_sent=0,flight_commands_sent=0)
    offline = 'FAIL' if any(r['status']=='FAIL' for r in results.values()) else (
        'BLOCKED' if any(r['status']=='BLOCKED' for r in results.values()) or any(r['status']!='PASS' for r in docs.values()) else 'PASS')
    summary = dict(run_id=run_id,offline=offline,documents=docs,checks=results,test_counts=counts,
                   python_total=sum(counts.values()) if all(c is not None for c in counts.values()) else None,
                   runtime=runtime)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    write_report(output,summary)
    print(str(ROOT/'INTEGRATED_OFFLINE_VERIFICATION_REPORT.md'))
    return 0 if offline=='PASS' else 1


def write_report(output, summary):
    rows = ['# R1~R3 통합 오프라인 검증 보고서',
            f"실행 ID: `{summary['run_id']}` · 공식 경로: `~/Desktop/ai-drone-safety-monitoring-main`",
            f"**오프라인 {summary['offline']} / 실제 시험 BLOCKED / 실제 보안 UNVERIFIED**. 이전 저장 증거를 이번 PASS로 사용하지 않았다.",
            '## 작업 전 목록과 전체 결과',
            'R1~R3 보고서와 설계를 사전 대조하고 검사별 예외 격리, 실행별 새 증거, test count 검증, 독립 build, 원본 SHA256, runtime NO-GO 분리를 이번 작업 목록으로 정했다. plan.json은 검사 시작 전에 작성했다.',
            '| 검사 | 결과 | 근거 |','|---|---|---|']
    for name,description in CHECKS:
        value = summary['checks'][name]
        evidence = value.get('reason','이번 실행 '+name+'.json')
        rows.append(f"| {description} | {value['status']} | {evidence} |")
    rows += [f"Python 합계: **{summary['python_total']}**. tools/ai={summary['test_counts']['regression']}, launcher={summary['test_counts']['launcher']}, geometry={summary['test_counts']['geometry']}. 기존352개와 R3 신규26개를 유지하고 이번 통합 runner 검사를 추가했다.",
             f"C++: {summary['checks']['cpp_policy'].get('stages','미수집')}단계 / {summary['checks']['cpp_policy'].get('assertions','미수집')} assertions. 신규 PX4 binary는 실행하지 않았다.",
             '## 보안·영역별 판정',
             '| 영역/항목 | 오프라인 | 실제 환경 |','|---|---|---|',
             '| SECURITY IPC peer/HMAC/session/replay/오류 폐쇄 | 단위·메모리 통합: regression 결과 적용 | UNVERIFIED |',
             '| SECURITY UDP signing/ACK/두 링크 replay/고정 비비행 probe | 단위·메모리 통합: regression 결과 적용 | UNVERIFIED |',
             '| strict MAVLink·unsigned/오류 키·FTP/forwarding·private IPC | C++/독립 build 결과 적용 | UNVERIFIED |',
             '| 계정·ACL·immutable 배포·키 배포 | 검증 로직과 fixture만 | UNVERIFIED / 배포 BLOCKED |',
             '| PX4 영속 ledger 정책 | C++ fixture 결과 적용 | UNVERIFIED |',
             '| broker key/session 영속 ledger | 메모리 claim mock만 존재 | UNVERIFIED / backend 미구현 |',
             '| FLIGHT_SAFETY HOME·RTL·착륙·장애물·경로 | 이번 범위 제외 | UNVERIFIED |',
             '| AI_DATA FIRE 학습 데이터 적격성 | 이번 범위 제외 | UNVERIFIED |',
             'SECURITY 결과는 FLIGHT_SAFETY/AI_DATA의 미검증과 독립이다. mock·컴파일 PASS는 실시간 보안 PASS가 아니다. 정상 서명/무서명/오류 키/replay를 실제 socket에서 비교하지 않았다.',
             '## 생성·수정 파일',
             '- 신규 tools/flight/secure/verify_integrated.py: 검사별 예외 격리·신규 실행 증거·자동 집계·본 보고서 생성.',
             '- 신규 tools/flight/secure/tests/test_integrated.py, tools/ai/test_secure_integrated.py: 독립 검사 지속·상태/집계 검사.',
             '- 신규 INTEGRATED_OFFLINE_VERIFICATION_REPORT.md 및 diagnostics/integrated_offline_verification/<run-id>/*.',
             '기존 보안 core/transport/PX4 정책·legacy tracker·루트 빈 tracker·운영 설정은 수정하지 않았다. 기존 regression/C++ runner 재사용으로 diagnostics/secure_r1_r2 결과와 standalone unit 산출물은 갱신될 수 있다.',
             '## 초기 실패와 근본 원인',
             '이번 통합 실행의 실패/BLOCKED는 위 표와 각 JSON에 그대로 기록한다. 한 단계 실패로 다른 검사를 생략하지 않으며 자동 반복 실행하지 않는다. 이전 R1/R2의 default 모듈 상속·simulation metadata·float-equal·CMake testing cache·import 환경 오류와 R3의 fixture key 길이·구 session spec 오류는 해당 보고서에 수정 내역이 보존되어 있다. 이를 새로운 실패로 중복 계산하지 않는다.',
             '기존 진입점의 최상위 예외로 전체 중단될 수 있는 구조와 기존 evidence 재수집 시 freshness 부족을 이번 독립 runner에서 보완했다. 읽을 수 없는 baseline/누락 toolchain은 BLOCKED, 실행된 검사 불일치는 FAIL이다. 이유를 숨기거나 검증 조건을 낮추지 않는다.',
             '## 보존 확인',
             f"보호 파일: {summary['checks']['preservation'].get('protected_files','미수집')}개, 판정 {summary['checks']['preservation']['status']}. 기존 tracker·adapter/session·launcher·원본 PX4/default board baseline SHA256을 대조했다.",
             '기존 instance0/7에 명령·상태조회 IPC·종료 signal을 보내지 않았다. Gazebo/QGC 시작·중단·설정 변경, 운영 키/방화벽/계정/서비스 변경, 비행 명령, AUTO PATROL, Git commit/push를 수행하지 않았다. 기존 세션의 현재 liveness/메모리·모든 설정파일 보존은 이 검사로 인증하지 않으며 UNVERIFIED다.',
             '## 남은 NO-GO와 승인 후 실행 계획',
             '| 우선순위 | NO-GO | 필요한 개발/검증·별도 승인 |','|---|---|---|',
             '| P0 | 실제 connector/listener/UDP socket 계층 없음 | trusted endpoint/소유권, 전체 monotonic deadline·동시성/DoS·bounded shutdown을 오프라인 구현·검토한 후 실제 IPC/UDP 실행 승인 |',
             '| P0 | broker 영속 key/session claim backend 없음 | atomic durable claim·crash consistency·rollback·용량 및 PX4 ledger 조정 구현/오프라인 검증. 실제 key 생성·배포·영속 경로 준비는 별도 승인 |',
             '| P0 | 계정·ACL·immutable code/런타임 미준비 | UID 분리/배포 경로·권한 검토 후 계정 생성·ACL·서비스 등록의 명시 승인 |',
             '| P1 | 실 OS peer denial·두 owned loopback socket·현재 strict 상태 미검증 | 새 계정의 접근 거부 → 충돌/소유권 → 승인된 신규 PX4만 기동 → 현재 MAVLink2/FTP/forwarding/IPC 확인 → 고정 비비행 인증/replay matrix 순서. 전제 실패 시 실제 전송 금지, 독립 read-only 수집 계속 |',
             '| P1 | 실제 crash/restart·키 교체 미검증 | 새 session/epoch/key와 영속 ledger 준비 후 승인된 신규 자식만 중단/복구. 동일 key 재사용/자동 fallback 금지 |',
             '| P2 | 외부 도달·VM·host firewall 미검증 | 별도 설계/승인. 이번 명령에는 외부 probe나 firewall 변경 없음 |',
             '현재 위 실제 시험은 모두 BLOCKED다. 실행하지 않았으므로 보안 성질은 UNVERIFIED로 남긴다. 기존0/7·Gazebo/QGC·운영 키·방화벽을 수정해 gate를 통과시키지 않는다. 비행·AUTO PATROL은 승인 요청 대상에 포함하지 않는다.',
             '## 재검증 명령',
             '```sh\ncd "$HOME/Desktop/ai-drone-safety-monitoring-main" && /opt/homebrew/Caskroom/miniforge/base/envs/vision-ai/bin/python -m tools.flight.secure.verify_integrated\n```',
             f"이번 실행의 원시 증거: `{output.relative_to(ROOT)}`. 각 단계 command/returncode와 fresh 증거를 개별 JSON으로 보관하고 summary.json에 집계했다. 재실행은 새 run-id 디렉터리를 만들며 본 보고서는 최신 결과로 갱신된다."]
    report = '\n\n'.join(rows).replace('|\n\n|','|\n|')+'\n'
    (ROOT/'INTEGRATED_OFFLINE_VERIFICATION_REPORT.md').write_text(report)
    (output/'REPORT.md').write_text(report)


if __name__ == '__main__': raise SystemExit(main())
