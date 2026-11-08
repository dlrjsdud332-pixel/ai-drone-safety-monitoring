"""Adapters around the unchanged legacy dashboard; all command output denied."""
import importlib.util
from pathlib import Path
import time
import subprocess
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]


def is_youtube_url(value):
    try:
        url = urlsplit(value)
        return (
            value.startswith("https://")
            and not any(char.isspace() or ord(char) < 32 for char in value)
            and url.netloc.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
            and len(url.path) > 1
        )
    except ValueError:
        return False


def resolve_youtube_video(value):
    result = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp", "--ignore-config",
            "--no-playlist", "--socket-timeout", "10",
            "-f", "best[height<=720]/bestvideo[height<=720]/best",
            "--get-url", "--", value,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        errors = [line for line in result.stderr.splitlines() if line.startswith("ERROR:")]
        raise RuntimeError(errors[-1] if errors else "YouTube 영상 주소 추출 실패")
    urls = result.stdout.splitlines()
    if len(urls) != 1 or not urls[0].startswith("https://"):
        raise RuntimeError("YouTube에서 재생 가능한 HTTPS 영상 주소를 받지 못했습니다.")
    return urls[0]


def load_legacy():
    spec=importlib.util.spec_from_file_location('integrated_legacy_tracker',ROOT/'legacy/drone_tracker.py')
    app=importlib.util.module_from_spec(spec);sys.modules[spec.name]=app;spec.loader.exec_module(app)
    resolve_legacy_source = app.resolve_source

    def resolve_source(value):
        if is_youtube_url(value):
            return resolve_youtube_video(value)
        return resolve_legacy_source(value)

    app.resolve_source = resolve_source
    return app


def install(app,model,source):
    class Telemetry(app.TelemetryState):
        def sim_heartbeat(self):pass
        def snapshot(self):
            value=super().snapshot();s=model.snapshot();d=s['data'] or {};p=d.get('position') or {};details=s.get('telemetry') or {}
            value.update(battery=d.get('battery') if d.get('battery') is not None else 0.,battery_valid=d.get('battery') is not None,lat=p.get('lat'),lon=p.get('lon'),home_lat=None,home_lon=None,home_status=details.get('home_status'),source_times=details.get('source_times',{}),flight_state=None,last_telemetry_received_at=s['last_good'],altitude_m=details.get('relative_altitude_m') if details.get('relative_altitude_m') is not None else details.get('altitude_msl_m',p.get('alt_msl')),flight_mode=source.packet.get('flight_mode','UNKNOWN') if s['fresh'] else 'UNKNOWN',telemetry_source='OFFLINE_DEMO',telemetry_connected=s['fresh'],telemetry_ever_connected=False,telemetry_age_s=s['age'] if s['age'] is not None else 999,armed=None)
            if getattr(source,'is_readonly_px4',False):
                from tools.control_ui.px4_readonly.source import project_telemetry
                value=project_telemetry(value,s,source)
            return value
    class Bridge:
        input_source='OFFLINE_DEMO'
        def __init__(self):self.last=-1e9
        def poll_telemetry(self):
            if time.monotonic()-self.last>=getattr(source,'poll_interval',1):
                self.last=time.monotonic()
                try:
                    source.packet=source.poll();model.update(source.client)
                    details=model.snapshot().get('telemetry')
                    if details is not None:
                        mission=details['mission'] or {}
                        source.packet=dict(flight_mode=details['flight_mode'] or 'UNKNOWN',mission='PX4 OBSERVATION',mission_status={2:'NOT STARTED',3:'ACTIVE',4:'PAUSED',5:'ITEMS COMPLETE',6:'FAILED'}.get(mission.get('state'),'UNKNOWN'),mission_progress=mission.get('progress'))
                except Exception:model.clear('BROKER_UNAVAILABLE');source.packet={}
        def send_command(self,*args,**kwargs):return dict(adapter='OBSERVATION_ONLY',connected=False,status='BLOCKED',command='HOLD',ack='NO_COMMAND_CHANNEL',sent_at=None)
        def snapshot(self):return dict(input_source=self.input_source,output_adapter='DENIED',adapter_connected=False,last_input_at=None,udp_telemetry_port=None)
        def close(self):pass
    class Mission:
        def update(self,*a,**kw):
            fresh=model.snapshot()['fresh'];p=source.packet if fresh else {}
            return dict(mission=p.get('mission','UNKNOWN'),status=p.get('mission_status','UNKNOWN'),progress=p.get('mission_progress'),mission_id=None)
    class Dashboard(app.Dashboard):
        def draw_status_panel(self,canvas,*args):
            if self.layout!=1:return
            from .presentation import panel
            panel(app,self,canvas,model,source)
        def mouse(self,event,x,y,flags,param):
            if event==app.cv2.EVENT_LBUTTONDOWN and hasattr(self,'camera_button'):
                a,b,c,d=self.camera_button
                if a<=x<c and b<=y<d:self.select_camera();return
            if event==app.cv2.EVENT_LBUTTONDOWN and hasattr(self,'telemetry_button'):
                a,b,c,d=self.telemetry_button
                if a<=x<c and b<=y<d:self.change_telemetry_connection();return
            if event==app.cv2.EVENT_LBUTTONDOWN and hasattr(self,'display_button'):
                a,b,c,d=self.display_button
                if a<=x<c and b<=y<d:
                    self.display_fill=not getattr(self,'display_fill',True)
                    return
            if event==app.cv2.EVENT_LBUTTONDOWN and hasattr(self,'details_button'):
                a,b,c,d=self.details_button
                if a<=x<c and b<=y<d:
                    self.show_observation_details=not getattr(self,'show_observation_details',False)
                    return
            if event==app.cv2.EVENT_LBUTTONDOWN and hasattr(self,'incident_agent') and getattr(self,'agent_hitbox_shape',None)==(app.WIDTH,app.HEIGHT,self.layout):
                for bounds,identity in getattr(self,'agent_hitboxes',[]):
                    a,b,c,d=bounds
                    if a<=x<c and b<=y<d:
                        if identity=='fold':self.agent_panel_collapsed=not getattr(self,'agent_panel_collapsed',False)
                        else:
                            from tools.control_agent.ui import open_review
                            open_review(self,identity)
                        return
            return super().mouse(event,x,y,flags,param)
        def change_telemetry_connection(self):
            from PySide6.QtWidgets import QFileDialog, QMessageBox, QInputDialog
            from tools.control_ui.px4_readonly.source import configuration, save_configuration
            if not hasattr(source, 'switch'):
                self.source_notice='READ-ONLY PX4 CONFIG REQUIRED AT STARTUP'
                return
            actions=['설정 불러오기', '현재 수신 설정 사본 저장 (연결 변경 없음)']
            action,ok=QInputDialog.getItem(None,'PX4 RX / 명령 차단','실장비 정보 UNKNOWN · SITL 설정을 실장비 정보로 사용하지 마세요.',actions,0,False)
            if not ok:source.cancel();self.source_notice=source.notice;return
            if action==actions[1]:
                name,_=QFileDialog.getSaveFileName(None,'수신 설정 사본 / 새 파일만 / 실장비 검증 증거 아님','','JSON (*.json)')
                if not name:self.source_notice='CONFIG SAVE CANCELLED / CONNECTION KEPT';return
                try:
                    save_configuration(name,source.config)
                    self.source_notice='CONFIG COPY SAVED / CONNECTION KEPT / 실제 연결 검증 대기'
                except Exception as exc:
                    self.source_notice='CONFIG SAVE FAILED / CONNECTION KEPT'
                    QMessageBox.warning(None,'설정 저장 실패','기존 파일은 덮어쓰지 않습니다. 새 경로·쓰기 권한·필수값을 확인하세요. 부분 파일은 연결에 사용하지 마세요.')
                return
            name,_=QFileDialog.getOpenFileName(None,'PX4 READ ONLY / commands blocked','','JSON (*.json)')
            if not name:source.cancel();self.source_notice=source.notice;return
            try:
                source.switch(configuration(name))
                self.source_notice='TELEMETRY CHANGE / PREVIOUS VALUES CLEARED'
            except Exception as exc:
                self.source_notice='TELEMETRY CONFIG ERROR / CURRENT SELECTION KEPT: '+str(exc)
                QMessageBox.warning(None,'읽기 전용 PX4 설정 확인',str(exc)+'\n실제 연결 검증 대기 · 비행 명령 차단')

        def add_source(self,stream=False):
            return super().add_source(stream, source_allowed=is_youtube_url if stream else None)
        def select_camera(self):
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            from tools.control_ui.camera_input import devices
            try:rows=devices()
            except Exception as exc:self.source_notice='CAMERA LIST ERROR: '+str(exc);return
            if not rows:self.source_notice='NO CAMERA AVAILABLE / CURRENT SOURCE KEPT: 카메라 연결·장치 목록·macOS 카메라 권한을 확인하세요. 실제 장치 연결 검증 대기.';return
            labels=[str(index)+' | '+name for index,name in rows]
            value,ok=QInputDialog.getItem(None,'Local camera / video only','Explicit camera selection; no microphone',labels,0,False)
            if not ok:self.source_notice='CAMERA CHANGE CANCELLED / CURRENT SOURCE KEPT';return
            self.replace_source(self.selected,str(rows[labels.index(value)][0]))

    return Telemetry,Bridge,Mission,Dashboard


class ObservationCommandBuilder:
    def build(self,*args,**kwargs):
        return dict(command='MONITOR',motion=False,params={'monitor_only':True},simulation=True,updated_at=time.time())


class ObservationValidator:
    def validate(self,command,*args,**kwargs):
        return dict(approved=False,reason='OBSERVATION_ONLY',safe_command=dict(command='MONITOR',motion=False,params={},simulation=True),original_command=command.get('command'),command='MONITOR',adjustments=[])


def make_agent_state(app,telemetry):
    """Keep legacy advisory rules, exclude ended/detached/stale camera events."""
    from .presentation import HazardJournal
    class State(app.AgentState):
        def __init__(self):
            super().__init__(telemetry);self.hazard_journal=HazardJournal()
        def update(self,channels):
            super().update(channels)
            active=self.hazard_journal.update(channels)
            with self.lock:
                for row in self.state['cameras']:
                    row['events']=[kind.replace(' ','_') for kind in active.get(row['camera'],[])]
                self.state['active_events']=sorted({kind for row in self.state['cameras'] for kind in row['events']})
                return self.state
    return State()


class ObservationDecisionEngine:
    """Read-only integration never proposes navigation or battery return actions."""
    def decide(self,state):
        events=state.get('active_events',[])
        return dict(primary_action='MONITOR',actions=['ALERT','RECORD'] if events else ['MONITOR'],reason='OBSERVATION_ONLY',priority=0,updated_at=time.time())


def observation_safety(app,telemetry,events):
    class Safety(app.SafetyController):
        def update(self,channel):
            if not telemetry.snapshot().get('battery_valid',False):
                self.last_level='UNKNOWN'
                return
            super().update(channel)
    return Safety(telemetry,events)
