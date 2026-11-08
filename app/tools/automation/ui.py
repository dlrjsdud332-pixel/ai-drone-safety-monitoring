"""Local reports and simulated route controls; no command adapter imported."""
import json
import time
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QPlainTextEdit,QPushButton,QHBoxLayout,QLineEdit
from PySide6.QtCore import QTimer,Qt

def patrol_guidance(snapshot, now):
    state = snapshot['state']
    observation = snapshot.get('observation', {})
    observed = observation.get('state')
    fresh = now < observation.get('valid_until', 0)
    if state in ('IDLE', 'COMPLETED', 'ABORTED'):
        return {
            'IDLE': ('설정 대기', '모의 경로를 설정하세요 [CONFIGURE]'),
            'COMPLETED': ('모의 미션 종료', '다음 모의 경로를 설정할 수 있습니다 [CONFIGURE]'),
            'ABORTED': ('모의 미션 중단', '중단 원인을 확인하고 새 모의 경로를 설정하세요 [CONFIGURE]'),
        }[state]
    if observation.get('reason') == 'SOURCE_CHANGED_NEW_SESSION_REQUIRED':
        return '판단 불가 · 입력 소스 변경', '현재 모의를 종료한 뒤 새 세션을 설정하세요'
    if observed == 'HOLD_RISK' and fresh:
        return '재개 차단 · 위험 관측', '사건을 검토하세요 [A]. 확인 후에도 위험 대기는 유지됩니다'
    if not fresh or observed in ('UNKNOWN', 'WAITING_INPUTS'):
        return '판단 불가 · 유효한 관측 없음', '영상·텔레메트리와 사건 관측 상태를 확인하세요'
    if observed == 'WAITING_CONFIRMATION':
        return '관찰 대기 · 작업자 탐지', '사건을 검토하고 확인하세요 [A]'
    if state in ('READY', 'PAUSED') and (
        snapshot['position'] is None or snapshot['battery'] is None or snapshot['battery'] <= 20
    ):
        return '진행 차단 · 위치 또는 배터리 조건 미충족', '모의 위치와 배터리 상태를 확인하세요'
    if state == 'READY' and observed == 'MONITORING':
        return '모의 시작 대기', '모의 순찰을 시작하세요 [START]'
    if state == 'PAUSED':
        if observed == 'RESUME_REQUESTED':
            return '모의 재개 요청 접수', '유효한 관측이 유지되는 동안 [START]를 눌러 재개하세요'
        if observed in ('ACKNOWLEDGED', 'MONITORING'):
            label = '작업자 확인 완료' if observed == 'ACKNOWLEDGED' else '모의 순찰 일시정지'
            return label, '모의 재개를 요청하세요 [REQUEST_RESUME]'
    if state in ('PATROLLING', 'RETURNING') and observed == 'MONITORING':
        return ('모의 순찰 중' if state == 'PATROLLING' else '모의 복귀 중'), '관측을 계속 확인하세요'
    return '모의 진행 대기', '현재 관측과 모의 상태를 확인하세요'


def patrol_status_text(snapshot, now):
    observation = snapshot.get('observation', {})
    label, action = patrol_guidance(snapshot, now)
    events = observation.get('events', [])
    evidence = ', '.join(f"CAM{event['camera']} {event['kind']} [{event['review']}]" for event in events)
    reason = observation.get('reason') or snapshot['reason']
    return (
        f"모의 미션 / 실제 PX4 명령 0 / SESSION {snapshot['cycle']}\n"
        f"{label}\n다음 조작: {action}\n"
        f"관측 근거: {evidence or '기록된 사건 없음'}\n"
        f"판단 코드: {reason or '—'} | 확인은 위험 해제 아님"
    )

def open_patrol(dashboard):
    old=getattr(dashboard,'sim_dialog',None)
    if old is not None and old.isVisible():old.raise_();return old
    sim=dashboard.automation.patrol;d=QDialog(dashboard.main_view.window);d.setWindowTitle('SIMULATED 순찰 / 실제 AUTO PATROL OFF');d.resize(620,420);layout=QVBoxLayout(d)
    layout.addWidget(QLabel('가상 local x,y,height(m) 경로 · 실제 GPS/비행 명령 아님\n탐지→관찰 대기 · [A] 확인은 위험 해제 아님 · REQUEST_RESUME은 요청만, START도 모의 전용\nJSON: {"route":[[0,0,5],[10,0,5],[0,0,5]],"period":10,"battery":100}'))
    status = QLabel()
    status.setTextFormat(Qt.TextFormat.PlainText)
    status.setWordWrap(True)
    status.setStyleSheet("background: #17283b; color: #f3f7fc; padding: 12px; border-radius: 6px;")
    layout.addWidget(status)
    edit=QPlainTextEdit();edit.setPlainText(json.dumps(dict(route=sim.route,period=sim.period,battery=sim.battery)));layout.addWidget(edit)
    notice=[None]
    def refresh():status.setText(patrol_status_text(sim.snapshot(),sim.clock())+('\n'+notice[0] if notice[0] else ''))
    buttons=QHBoxLayout();layout.addLayout(buttons);d.controls={}
    def apply(name):
        try:
            dashboard.automation.observe_patrol(dashboard.channels)
            if name=='CONFIGURE':
                value=json.loads(edit.toPlainText());sim.configure(value['route'],value['period'],value['battery'])
            elif name=='REQUEST_RESUME':sim.request_resume(sim.cycle)
            else:getattr(sim,dict(START='start',PAUSE='pause',ABORT='abort',END='finish',POSITION_LOSS='lose_position')[name])()
            notice[0]=None;refresh()
        except Exception as e:notice[0]='실패 / '+str(e);refresh()
    for name in ('CONFIGURE','START','PAUSE','REQUEST_RESUME','ABORT','END','POSITION_LOSS'):
        b=QPushButton(name);b.clicked.connect(lambda checked=False,n=name:apply(n));buttons.addWidget(b);d.controls[name]=b
    timer=QTimer(d);timer.timeout.connect(refresh);timer.start(250);refresh();dashboard.sim_dialog=d;d.show();return d

def report_text(dashboard,identity):
    auto=getattr(dashboard,'automation',None)
    if auto is None:return ''
    auto.sync()
    try:p=auto.reports.get(identity)
    except Exception:return '\nREPORT BLOCKED / 저장 보고서 검증 실패'
    if auto.reports.status!='OK':return '\n자동 보고서 저장 실패 / REPORT BLOCKED · 사건 감사 기록과 별도 상태'
    if p is None:return '\n자동 보고서 UNKNOWN'
    return '\n\n--- 자동 보고서 (OFFLINE_DEMO) ---\n'+auto.reports.render(p)+'\n보고서 경로: '+str(auto.reports.root/auto.reports.key(identity))

def overlay(app,dashboard,image):
    from tools.integration.presentation import text,TEAL,MUTED
    auto=dashboard.automation;s=auto.patrol.snapshot()
    if auto.flight is not None:
        f=auto.flight.snapshot();t=f['telemetry'] or {}
        progress=f"{t['progress_pct']:.1f}%" if t.get('progress_pct') is not None else 'UNKNOWN'
        label=f"SIMULATED PX4 {f['state']} | {f['connection']} | WP {t.get('waypoint','UNKNOWN')} | {progress} [A]"
        detail='HOME '+str(t.get('home','UNKNOWN'))+' | SPEED '+str(t.get('speed_m_s','UNKNOWN'))+' m/s | REPORT '+auto.reports.status
    else:
        label=f"SIMULATED PATROL {s['state']} {s['progress']:.0f}% | [T] SIM | REPORT {auto.reports.status} [A]"
        pos=s['position'];detail='LOCAL XYZ '+(str([round(v,1) for v in pos]) if pos is not None else 'UNKNOWN')+' m | SIM BAT '+(f"{s['battery']:.1f}%" if s['battery'] is not None else 'UNKNOWN')+f" | SIM TIME {s['elapsed']:.1f}s"
    detail+=' | DATA '+auto.hub_status+' [J] | GAZEBO UNVERIFIED'
    from tools.integration.presentation import draw_tile_information
    for index in ([dashboard.selected] if dashboard.layout==1 else range(dashboard.layout)):
        draw_tile_information(app,dashboard,image,index)
    # The footer uses the same guidance as the detailed patrol panel.
    if auto.flight is None:
        label, action = patrol_guidance(s, auto.patrol.clock())
        dashboard.shared_sim_summary = f"[T] 모의 미션 · {label} | {action}"
    else:
        dashboard.shared_sim_summary = label + ' | ' + detail



def open_report(dashboard,identity):
    from PySide6.QtWidgets import QTextBrowser
    from PySide6.QtCore import QUrl
    auto=dashboard.automation;auto.sync()
    if auto.reports.status!='OK':return None
    p=auto.reports.get(identity)
    if p is None:return None
    d=QDialog(dashboard.agent_review_dialog);d.resize(780,680);d.setWindowTitle('OFFLINE_DEMO 자동 사건 보고서 · 증거')
    layout=QVBoxLayout(d);browser=QTextBrowser();browser.setOpenExternalLinks(False);browser.setOpenLinks(False)
    page=auto.reports.root/(auto.reports.key(identity)+'.html');browser.document().setBaseUrl(QUrl.fromLocalFile(str(page)))
    browser.setHtml(page.read_text());layout.addWidget(browser);d.show();dashboard.report_dialog=d;return d


def open_data_center(dashboard):
    d=QDialog(dashboard.main_view.window);d.resize(850,600);d.setWindowTitle('LOCAL DATA CENTER / 관측 기록 · 비행 권한 없음')
    layout=QVBoxLayout(d);layout.addWidget(QLabel('드론 ID / 미션 ID / 사건 ID 정확 일치 검색 · 과거 기록은 현재 센서값이 아닙니다'))
    fields=[]
    for label in ('drone_id','mission_id','incident_id'):
        edit=QLineEdit();edit.setPlaceholderText(label);layout.addWidget(edit);fields.append((label,edit))
    output=QPlainTextEdit();output.setReadOnly(True);button=QPushButton('검색 (최근 100건)');layout.addWidget(button);layout.addWidget(output)
    def search():
        try:output.setPlainText(json.dumps(dashboard.automation.hub.search(**{k:e.text().strip() for k,e in fields if e.text().strip()}),ensure_ascii=False,indent=2))
        except Exception:output.setPlainText('DATA CENTER BLOCKED')
    button.clicked.connect(search);search();d.show();dashboard.data_center_dialog=d;return d
