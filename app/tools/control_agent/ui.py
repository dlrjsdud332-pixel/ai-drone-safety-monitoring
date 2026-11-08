"""Qt incident workflow. Human review is separate from detection and execution."""
import time
from .core import TRANSITIONS

SHORT={'FIRE':'현장 확인·안전 담당자 통보 권고','SMOKE':'발화 여부 확인·현장 점검 권고','FALL':'작업자 상태·응급 절차 확인 권고','NO-Hardhat':'안전모 착용 확인 권고','NO-Safety Vest':'안전조끼 착용 확인 권고','DANGER ZONE':'접근 위험 확인·관리자 알림 권고','PERSON':'사람 관찰 / 사고 판단 아님'}
MEANING={'PENDING':'미처리 · 담당자 확인 대기','APPROVED':'확인 · 검토 수용 (위험 해제·현장 조치 완료 아님)','HELD':'보류 · 미해결 / 추가 확인 필요','REJECTED':'기각 · 검토 기각 (탐지 기록 삭제 아님)'}


def observation_status(row):
 """Describe evidence availability, never infer that a physical risk cleared."""
 if row['state']=='CURRENT':return '위험 관측 지속 / 실제 위험 해제 미확인'
 reason=row.get('end_reason','UNKNOWN')
 meanings={'EVIDENCE_EXPIRED':'관측 만료·STALE','SOURCE_UNAVAILABLE':'영상 끊김·종료','SOURCE_CHANGED':'소스 변경','APP_RESTART_UNVERIFIED':'앱 재시작·재검증 대기','CAPACITY_EVICTED':'관측 목록 용량 제한'}
 return meanings.get(reason,reason)+' / 현재 위험 UNKNOWN · 해제 미확인'


def panel_rows(agent):
 s=agent.snapshot();current=s['current'];recent=sorted(s['history'],key=lambda r:r['last_seen'],reverse=True)[:3]
 selected=current[0] if current else None
 return dict(severity=s['severity'],count=len(current),recommendation=SHORT[selected['kind']] if selected else '새 유효 위험 근거 없음',review=selected['review'] if selected else 'NONE',urgent_id=selected['id'] if selected else None,current=[f"CAM{r['camera_id']} {r['kind']} [{r['review']}]" for r in current[:3]],recent=[f"CAM{r['camera_id']} {r['kind']} [{r['review']}]" for r in recent],held=s['unresolved_counts'].get('HELD',0),pending=s['unresolved_counts'].get('PENDING',0),audit=s['audit_status'],requires_human=True,execution_authorized=False)


def notify(dashboard,message):
 from PySide6.QtCore import QTimer,Qt
 from PySide6.QtWidgets import QLabel
 old=getattr(dashboard,'agent_toast',None)
 if old is not None:old.deleteLater()
 toast=QLabel(message,dashboard.main_view.window);toast.setWordWrap(True);toast.setStyleSheet('background:#234452;color:#d7fff3;padding:10px;border:1px solid #5cc7be;');toast.setAttribute(Qt.WA_TransparentForMouseEvents)
 width=min(720,max(260,dashboard.main_view.window.width()-40));toast.resize(width,65);toast.move(20,125 if hasattr(dashboard,'automation') else 80);toast.show();toast.raise_();dashboard.agent_toast=toast
 dashboard.agent_notice=dict(text=message,until=time.monotonic()+7,success=True)
 timer=QTimer(toast);timer.setSingleShot(True);timer.timeout.connect(toast.hide);timer.start(7000)


def open_review(dashboard,incident_id=None):
 from PySide6.QtCore import Qt,QTimer
 from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QComboBox,QLineEdit,QPlainTextEdit,QHBoxLayout,QPushButton
 existing=getattr(dashboard,'agent_review_dialog',None)
 if existing is not None and existing.isVisible():
  if incident_id:existing.load(incident_id)
  existing.raise_();return existing
 agent=dashboard.incident_agent
 class ReviewDialog(QDialog):
  def __init__(self):
   super().__init__(dashboard.main_view.window);self.offset=0;self.rows=[];self.setWindowTitle('사건 검토·이력 / 비행 권한 없음');self.resize(700,600);self.setAttribute(Qt.WA_DeleteOnClose)
   layout=QVBoxLayout(self)
   layout.addWidget(QLabel('확인: 검토 수용·위험 해제 아님 | 보류: 미해결·추가 확인 | 기각: 검토 기각\n모든 상태는 현장 조치 완료·사고 확정·PX4/비행 승인이 아닙니다.'))
   top=QHBoxLayout();layout.addLayout(top);self.scope=QComboBox()
   for text,value in (('미처리·보류','UNRESOLVED'),('현재 탐지','CURRENT'),('과거 관측 이력','HISTORY'),('전체 사건','ALL')):self.scope.addItem(text,value)
   top.addWidget(self.scope);self.previous=QPushButton('이전 100');self.more=QPushButton('다음 100');top.addWidget(self.previous);top.addWidget(self.more)
   self.combo=QComboBox();layout.addWidget(self.combo);self.info=QPlainTextEdit();self.info.setReadOnly(True);layout.addWidget(self.info)
   if getattr(dashboard,'automation',None) is not None:
    self.report_button=QPushButton('자동 보고서 / 증거 프레임 열람');layout.addWidget(self.report_button)
    def show_report():
     from tools.automation.ui import open_report
     try:
      if open_report(dashboard,self.combo.currentData()) is None:self.status.setText('REPORT BLOCKED / 보고서 저장 상태 확인')
     except Exception:self.status.setText('보고서 열람 실패 · 성공 아님')
    self.report_button.clicked.connect(show_report)
   layout.addWidget(QLabel('담당자 ID · 로컬 입력 (본인 인증 아님)'));self.operator=QLineEdit();self.operator.setObjectName('review_operator');layout.addWidget(self.operator)
   self.note=QPlainTextEdit();self.note.setObjectName('review_reason');self.note.setPlaceholderText('처리 사유 필수 · 1~400자');self.note.setMaximumHeight(85);layout.addWidget(self.note)
   self.status=QLabel('기록 성공 전 상태는 변경되지 않습니다.');self.status.setWordWrap(True);layout.addWidget(self.status)
   buttons=QHBoxLayout();layout.addLayout(buttons);self.buttons={}
   for caption,value in (('확인','APPROVED'),('보류','HELD'),('기각','REJECTED')):
    b=QPushButton(caption);b.setObjectName('review_'+value);b.clicked.connect(lambda checked=False,v=value:self.record(v));buttons.addWidget(b);self.buttons[value]=b
   self.combo.currentIndexChanged.connect(lambda _:self.describe());self.scope.currentIndexChanged.connect(self.change_scope);self.previous.clicked.connect(lambda:self.page(-100));self.more.clicked.connect(lambda:self.page(100))
   self.setStyleSheet('QDialog {background:#111b29;color:#e7edf3;} QLabel {color:#e7edf3;} QLineEdit,QPlainTextEdit,QComboBox {background:#1b2a3c;color:#e7edf3;padding:6px;} QPushButton {background:#234452;color:#c0e8e5;padding:8px;} QPushButton:disabled {color:#747d85;}')
   self.load(incident_id)
   self.timer=QTimer(self);self.timer.timeout.connect(lambda:self.describe(False));self.timer.start(500)
  def change_scope(self):self.offset=0;self.load()
  def page(self,delta):self.offset=max(0,self.offset+delta);self.load()
  def load(self,selected=None):
   self.rows=agent.catalog(self.scope.currentData(),self.offset,100)
   if selected and not any(r['id']==selected for r in self.rows):
    self.scope.blockSignals(True);self.scope.setCurrentIndex(self.scope.findData('ALL'));self.scope.blockSignals(False);self.offset=0
    self.rows=agent.catalog('ALL',0,100)
    row=agent.lookup(selected)
    if row and not any(r['id']==selected for r in self.rows):self.rows.insert(0,row)
   self.combo.blockSignals(True);self.combo.clear()
   for r in self.rows:self.combo.addItem(f"{r['id']} | CAM{r['camera_id']} | {r['kind']} | {MEANING[r['review']]}",r['id'])
   if selected:self.combo.setCurrentIndex(max(0,self.combo.findData(selected)))
   self.combo.blockSignals(False);self.previous.setEnabled(self.offset>0);self.more.setEnabled(len(self.rows)>=100);self.describe()
  def describe(self,reset_inputs=True):
   row=agent.lookup(self.combo.currentData())
   if reset_inputs:self.operator.clear();self.note.clear()
   terminal=row is None or not TRANSITIONS[row['review']] or agent.snapshot()['audit_status']!='OK'
   for target,b in self.buttons.items():b.setEnabled(not terminal and target in TRANSITIONS[row['review']])
   self.operator.setReadOnly(terminal);self.note.setReadOnly(terminal)
   if row is None:self.info.setPlainText('해당 사건 없음 / 저장소 오류는 AUDIT BLOCKED로 표시됩니다.');return
   e=row['evidence'][-1];score='UNKNOWN' if e['confidence'] is None else str(e['confidence'])
   stamp=lambda v:time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(v))
   lines=[f"{row['id']} / CAM{row['camera_id']} / {row['kind']} / {row['severity']}",f"탐지 상태: {row['state']} {row.get('end_reason','')}",observation_status(row),f"처리 상태: {MEANING[row['review']]}",f"첫 관측: {stamp(row['first_seen'])} / 마지막: {stamp(row['last_seen'])}",f"관측 지속: {row['observed_duration']:.2f}s (실제 위험 해소 판단 아님)",f"출처: {row['source_kind']} / 연결 ID: {row['source_id']} / 점수: {score} ({e['evidence_method']}, 사고 확률 아님)",row['reason'],'추천: '+row['recommendation'],'현장 조치 수행 여부: UNKNOWN (보고 없음)',f"위치: {e['location'] if e['location'] is not None else 'UNKNOWN'} / 구역: {e['zone'] if e['zone'] is not None else 'UNKNOWN'}"]
   for review in row['reviews']:lines.append(f"{stamp(review['at'])} | {review['operator']} | {review['status']} | {review['note']}")
   from tools.automation.ui import report_text
   lines.append(report_text(dashboard,row['id']))
   self.info.setPlainText('\n'.join(lines))
   if reset_inputs:self.status.setText('읽기 전용 · 중복 처리 불가' if terminal else '담당자 ID와 사유를 입력하세요. 보류는 미해결로 유지됩니다.')
   if terminal and row['reviews']:self.operator.setText(row['reviews'][-1]['operator']);self.note.setPlainText(row['reviews'][-1]['note'])
  def record(self,target):
   identity=self.combo.currentData()
   try:receipt=agent.review(identity,target,self.operator.text(),self.note.toPlainText())
   except Exception:
    self.status.setText('처리 실패 · 원래 상태 유지. 입력·중복 처리·AUDIT 저장 상태를 확인하세요.');return
   message=f"{identity} {MEANING[target]} · 기록 저장 완료 · 실행 권한 없음"
   if getattr(dashboard,'automation',None) is not None:
    dashboard.automation.sync()
    if dashboard.automation.reports.status!='OK':message+=' · 사건 저장 성공 / 보고서 갱신 실패 REPORT BLOCKED'
   dashboard.agent_last_review=dict(incident_id=identity,**receipt);notify(dashboard,message)
   candidates=[r for r in agent.catalog('UNRESOLVED') if r['review']=='PENDING' and r['id']!=identity]
   if candidates:
    self.scope.blockSignals(True);self.scope.setCurrentIndex(self.scope.findData('UNRESOLVED'));self.scope.blockSignals(False)
    self.offset=0;self.load(candidates[0]['id']);self.status.setText(message+'\n다음 미처리 사건으로 이동했습니다.')
   else:self.accept()
 dialog=ReviewDialog();dashboard.agent_review_dialog=dialog
 def clear(*args):
  if getattr(dashboard,'agent_review_dialog',None) is dialog:dashboard.agent_review_dialog=None
 dialog.finished.connect(clear);dialog.show();return dialog


def compact(app,dashboard,image,top):
 """Use existing footer only: no video area or camera control hitbox changes."""
 from tools.integration.presentation import text,NAVY,CARD,TEAL,MUTED,FIRE
 cv=app.cv2;p=panel_rows(dashboard.incident_agent);width=app.WIDTH
 cv.rectangle(image,(0,top),(width,app.HEIGHT),NAVY,-1)
 collapsed=getattr(dashboard,'agent_panel_collapsed',False)
 cv.rectangle(image,(width-130,top+5),(width-8,top+31),CARD,-1)
 text(cv,image,'AI [+] G' if collapsed else 'AI [-] G',width-122,top+24,110,.36,TEAL)
 heading=f"AI {p['severity']} | CURRENT {p['count']} | HELD {p['held']} | AUDIT {p['audit']}"
 text(cv,image,heading,12,top+23,width-155,.42,FIRE if p['severity']=='HIGH' else TEAL)
 detail='과거 이력·담당자 검토 [A] / 입력 만료는 위험 해소 아님' if collapsed else ((p['current'][0]+' | ') if p['current'] else 'CURRENT UNKNOWN | ')+p['recommendation']+' [A]'
 text(cv,image,detail,12,top+49,width-25,.37,TEAL)
 text(cv,image,'HISTORY: '+(p['recent'][0] if p['recent'] else 'NONE')+' | AUTO OFF / NO EXECUTION',12,top+70,width-25,.30,MUTED)
 dashboard.agent_hitbox_shape=(app.WIDTH,app.HEIGHT,dashboard.layout)
 dashboard.agent_hitboxes=[((width-130,top+5,width-8,top+31),'fold'),((0,top,width-140,app.HEIGHT),p['urgent_id'])]
