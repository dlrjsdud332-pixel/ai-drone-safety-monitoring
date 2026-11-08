"""Run unchanged AI vision/UI with isolated, observation-only demo telemetry."""
import argparse
import json
import math
import os
from pathlib import Path
import time
from .demo import DemoSource,require_offline
from .observation import ObservationModel


def gazebo_source(value):
    from tools.gazebo.camera_bridge import configuration
    try:configuration(value)
    except (ValueError, TypeError) as error:raise argparse.ArgumentTypeError(str(error)) from error
    return value


def video_sources(local, gazebo, root, camera_index=None):
    sources=[str(p.resolve()) for p in (local or ([] if gazebo or camera_index is not None else [root/'videos/14297188_1920_1080_24fps.mp4']))]
    if any(not Path(p).is_file() for p in sources):raise ValueError('Provide existing local video files')
    if camera_index is not None:
        if type(camera_index) is not int or camera_index<0:raise ValueError('Explicit nonnegative camera index required')
        if gazebo:raise ValueError('Choose one primary live camera input')
        sources.insert(0,str(camera_index))
    if gazebo:sources.insert(0,gazebo)
    if not 1<=len(sources)<=16:raise ValueError('Provide 1–16 video sources in total')
    return sources


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('OFFLINE_DEMO','SECURE_LIVE'),default='OFFLINE_DEMO')
    parser.add_argument('--source',type=Path,action='append',help='Local video file; repeated sources allowed')
    parser.add_argument('--gazebo-source',type=gazebo_source,help='Image-only gazebo-ssh:// URL with explicit Ubuntu project and current partition; channel 1, followed by any --source files')
    parser.add_argument('--camera-index',type=int,help='Explicit Mac video-only AVFoundation camera index; no microphone or screen capture')
    parser.add_argument('--px4-readonly-config',type=Path,help='Read-only telemetry JSON: pinned SSH instance0 or dedicated receive-only UDP; no command channel')
    parser.add_argument('--telemetry-replay',type=Path,help='OFFLINE_DEMO exporter-record JSON; never live proof')
    parser.add_argument('--native-export-fixture',action='store_true',help='Offline uORB exporter output fixture, no PX4 execution')
    parser.add_argument('--telemetry-fixture',action='store_true',help='Offline authenticated PX4 message fixture; never a live connection')
    parser.add_argument('--incident-store',type=Path,help='Project data/offline_demo/*.sqlite3; defaults to persistent incident agent history')
    parser.add_argument('--import-agent-audit',type=Path,help='Read-only import of prior project diagnostics incident_audit.jsonl')
    parser.add_argument('--offline-flight-scenario',action='store_true',help='Virtual PX4 mission only; no PX4/transport, fixture approvals')
    parser.add_argument('--flight-visual',action='store_true',help='Display-only fixture route window; requires offline-flight-scenario')
    parser.add_argument('--flight-time-scale',type=float,help='Fixture duration scale 1..6; visual default3 (about30seconds)')
    parser.add_argument('--capture-flight',action='store_true',help='Save fixture phase screenshots with flight-visual')
    parser.add_argument('--no-legacy-import',action='store_true',help='Do not auto-import previous demo audits into this run')
    parser.add_argument('--sim-route',type=Path,help='Explicit OFFLINE_DEMO route JSON: route,period,battery; starts SIM PATROL only')
    parser.add_argument('--device',default='cpu');parser.add_argument('--seconds',type=float,default=0)
    parser.add_argument('--offscreen',action='store_true');parser.add_argument('--capture-layouts',action='store_true')
    parser.add_argument('--capture-window-sizes',action='store_true',help='Local GUI review: capture 4-view at two window sizes')
    parser.add_argument('--capture-details',action='store_true',help='Local GUI review: capture the existing B details panel')
    parser.add_argument('--capture-fit',action='store_true',help='Local GUI review: capture the full-video fit mode')
    args=parser.parse_args()
    if args.px4_readonly_config and any((args.telemetry_fixture,args.native_export_fixture,args.telemetry_replay,args.sim_route,args.offline_flight_scenario,args.flight_visual)):
        parser.error('Read-only PX4 cannot mix with synthetic telemetry/flight/patrol')
    if args.flight_visual and not args.offline_flight_scenario:parser.error('flight-visual requires offline-flight-scenario')
    if args.capture_flight and not args.flight_visual:parser.error('capture-flight requires flight-visual')
    flight_scale=args.flight_time_scale if args.flight_time_scale is not None else (3. if args.flight_visual else 1.)
    if not math.isfinite(flight_scale) or not 1<=flight_scale<=6:parser.error('flight-time-scale must be 1..6')
    try:require_offline(args.mode)
    except ValueError as e:parser.error(str(e))
    from .dashboard import ROOT,load_legacy,install,ObservationCommandBuilder,ObservationValidator,make_agent_state,ObservationDecisionEngine,observation_safety
    try:sources=video_sources(args.source,args.gazebo_source,ROOT,args.camera_index) if args.camera_index is not None else video_sources(args.source,args.gazebo_source,ROOT)
    except ValueError as error:parser.error(str(error))
    if not math.isfinite(args.seconds) or args.seconds<0:parser.error('seconds must be finite and nonnegative')
    required=('yolo11s.pt','yolo11n-pose.pt','ppe_best.pt')
    if any(not (ROOT/'models'/name).is_file() for name in required):parser.error('Required local models missing; automatic model download is disabled')
    os.environ['YOLO_AUTOINSTALL']='false'
    if args.offscreen:os.environ['QT_QPA_PLATFORM']='offscreen'
    # Python sockets remain denied; optional camera-only SSH runs through the existing subprocess reader.
    import socket
    import ssl  # Load its socket subclass before installing the deny guard.
    def denied(*a,**kw):raise RuntimeError('Integration entrypoint forbids real sockets')
    class DeniedSocket(socket.socket):
        def __new__(cls,*a,**kw):return denied()
    socket.socket=DeniedSocket;socket.create_connection=denied
    app=load_legacy()
    from .presentation import install_rendering,overlays
    install_rendering(app)
    from datetime import datetime,timezone
    out=ROOT/'diagnostics/integration_ui'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ');out.mkdir(parents=True)
    app.MODEL_ROOT=ROOT/'models';app.ROOT=out
    from tools.control_agent import IncidentAgent
    from tools.control_agent.store import IncidentStore,BlockedAudit
    from tools.control_agent.integration import attach
    store_path=(args.incident_store or ROOT/'data/offline_demo/incident_agent/incidents.sqlite3').absolute()
    if not (ROOT/'data/offline_demo').resolve().is_relative_to(ROOT.resolve()) or store_path.is_symlink() or not store_path.resolve().is_relative_to((ROOT/'data/offline_demo').resolve()) or store_path.suffix!='.sqlite3':parser.error('Incident store must stay inside project data/offline_demo')
    incident_store=None;imported_from=None;store_error=None
    try:
        incident_store=IncidentStore(store_path)
        legacy=args.import_agent_audit
        if legacy is None and incident_store.empty() and not args.no_legacy_import:
            candidates=list((ROOT/'diagnostics/integration_ui').glob('*/incident_audit.jsonl'))
            if candidates:legacy=max(candidates,key=lambda p:p.stat().st_mtime)
        if legacy is not None:
            if not (ROOT/'diagnostics').resolve().is_relative_to(ROOT.resolve()) or legacy.is_symlink() or not legacy.resolve().is_relative_to((ROOT/'diagnostics').resolve()) or legacy.name!='incident_audit.jsonl':raise ValueError('Invalid legacy audit path')
            incident_store.import_jsonl(legacy);imported_from=str(legacy)
    except Exception as error:
        if incident_store is not None:incident_store.close()
        store_error=type(error).__name__;incident_store=BlockedAudit('incident store restore/import failed')
    incident_agent=IncidentAgent(audit=incident_store)
    evidence_bridge=attach(app,incident_agent,mission_context=lambda:automation.patrol.cycle)
    from tools.automation.runtime import Automation
    automation=Automation(incident_agent,store_path.parent/(store_path.stem+'_automation'))
    automation.event_bridge=evidence_bridge
    if args.offline_flight_scenario and not args.flight_visual:
        from tools.flight.autonomy.scenario import OfflineFlightScenario
        automation.flight=OfflineFlightScenario(automation.reports.patrol,time_scale=flight_scale)
    automation.sync()
    if args.sim_route:
        try:
            if args.sim_route.stat().st_size>16384:raise ValueError('route size')
            route=json.loads(args.sim_route.read_text());automation.patrol.configure(route['route'],route['period'],route['battery']);automation.patrol.start()
        except Exception as e:parser.error('Invalid explicit simulation route: '+str(e))
    if sum((bool(args.telemetry_fixture),bool(args.native_export_fixture),bool(args.telemetry_replay)))>1:parser.error('Choose one offline telemetry source')
    if args.px4_readonly_config:
        from tools.control_ui.px4_readonly.source import configuration
        from tools.control_ui.px4_readonly.selection import ReadOnlySelection
        try:
            source=ReadOnlySelection(configuration(args.px4_readonly_config));source.start()
        except (ValueError,KeyError,OSError,RuntimeError) as error:parser.error('Read-only connection rejected: '+str(error))
    elif args.telemetry_replay:
        from .telemetry_replay import ExportReplaySource
        try:source=ExportReplaySource.from_file(args.telemetry_replay,mode=args.mode)
        except Exception:parser.error('Offline replay rejected: schema, source or timeline invalid')
    elif args.native_export_fixture:
        from .native_fixture import NativeFixtureSource
        source=NativeFixtureSource()
    elif args.telemetry_fixture:
        from .px4_fixture import Px4FixtureSource
        source=Px4FixtureSource()
    else:source=DemoSource()
    source.packet={};model=source.model if getattr(source,'is_readonly_px4',False) else ObservationModel();Telemetry,Bridge,Mission,Dashboard=install(app,model,source)
    automation.observation_provider=model.snapshot
    telemetry=Telemetry();events=app.EventManager(telemetry)
    channels=[app.Channel(i+1,sources[i] if i<len(sources) else '',True,events) for i in range(app.MAX_CAMERAS)]
    evidence_bridge.sync_channels(channels)
    for c in channels:
        c.auto_control.set_enabled(False)
        c.auto_control.set_enabled=lambda enabled:None  # No enabling path in this entrypoint.
    analyzer=app.Analyzer(channels,args.device);bridge=Bridge();agent_state=make_agent_state(app,telemetry)
    dashboard=Dashboard(channels,analyzer,telemetry,observation_safety(app,telemetry,events),app.TelemetryInputAdapter(telemetry),agent_state,ObservationDecisionEngine(),ObservationCommandBuilder(),ObservationValidator(),bridge,Mission())
    dashboard.display_fill=True
    dashboard.hazard_journal=agent_state.hazard_journal
    dashboard.automation=automation
    dashboard.incident_agent=incident_agent;dashboard.evidence_bridge=evidence_bridge
    class View(app.MainView):
        def update(self,image):
            overlays(app,dashboard,image,model,source)
            super().update(image)
    dashboard.ensure_qt();dashboard.main_view=View(dashboard);analyzer.start();start=time.monotonic();captured=set();frames=0
    flight_visual=None;flight_captures=[];flight_seen=set()
    if args.flight_visual:
        from tools.flight.autonomy.scenario import OfflineFlightScenario
        automation.flight=OfflineFlightScenario(automation.reports.patrol,time_scale=flight_scale)
        from tools.automation.flight_visual import FlightVisual
        flight_visual=FlightVisual(dashboard);flight_visual.show()
    print(source.source_label+': commands denied; no demo fallback' if args.px4_readonly_config else 'OFFLINE_DEMO: fixture telemetry; flight commands denied; AUTO OFF; video=' + ('LIVE WEBCAM (video only)' if args.camera_index is not None else 'GAZEBO SSH (image only)' if args.gazebo_source else 'LOCAL FILE'))
    try:
        while not dashboard.main_view.closed:
            automation.update(channels)
            dashboard.draw();frames+=1
            if flight_visual is not None and args.capture_flight:
                flight=automation.flight.snapshot();phase=flight['state'];progress=(flight['telemetry'] or {}).get('progress_pct')
                name=phase
                if phase=='PATROLLING' and progress is not None:name+='_'+('late' if progress>=70 else 'mid' if progress>=30 else 'start')
                if name not in flight_seen:
                    flight_visual.repaint();file=out/('flight_'+name+'.png')
                    if not flight_visual.grab().save(str(file)):raise OSError('flight screenshot failed')
                    flight_seen.add(name);flight_captures.append(dict(phase=phase,file=file.name,telemetry=flight['telemetry']))
            if args.capture_layouts:
                layout=(1,2,4)[min(2,int((time.monotonic()-start)//2))];dashboard.set_layout(layout)
                if layout not in captured:
                    dashboard.draw();dashboard.main_view.window.grab().save(str(out/f'layout_{layout}.png'));captured.add(layout)
            if args.capture_window_sizes and time.monotonic()-start>=6 and not getattr(dashboard,'size_captured',False):
                dashboard.set_layout(4);dashboard.main_view.window.showNormal()
                for w,h in ((1600,900),(1280,720)):
                    dashboard.main_view.window.resize(w,h);dashboard.dialog_app.processEvents()
                    dashboard.draw();dashboard.main_view.window.grab().save(str(out/f'window_{w}x{h}.png'))
                dashboard.size_captured=True
            if args.capture_details and time.monotonic()-start>=8 and not getattr(dashboard,'details_captured',False):
                dashboard.show_observation_details=True;dashboard.draw()
                dashboard.main_view.window.grab().save(str(out/'details_open.png'))
                dashboard.show_observation_details=False;dashboard.details_captured=True
            if args.capture_fit and time.monotonic()-start>=9 and not getattr(dashboard,'fit_captured',False):
                dashboard.display_fill=False;dashboard.draw()
                dashboard.main_view.window.grab().save(str(out/'fit_full_view.png'))
                dashboard.display_fill=True;dashboard.fit_captured=True
            key=dashboard.key_queue.popleft() if dashboard.key_queue else -1
            if key in (27,ord('q')):break
            if key==ord('v'):dashboard.set_layout({1:2,2:4,4:1}.get(dashboard.layout,1))
            if key==ord('f'):dashboard.main_view.toggle_fullscreen()
            if ord('1')<=key<=ord('4'):dashboard.select(key-ord('1'))
            if key in map(ord,'szmdr'):
                name=dict(zip(map(ord,'szmdr'),('SELECT','ZOOM','MOVE','DANGER','RESET')))[key];dashboard.mouse(app.cv2.EVENT_LBUTTONDOWN,15+app.TOOLS.index(name)*105,10,0,None)
            c=channels[dashboard.selected]
            if key==ord('p'):c.show_ppe=not c.show_ppe
            if key==ord('n'):dashboard.show_map=not dashboard.show_map
            if key==ord('g'):dashboard.agent_panel_collapsed=not getattr(dashboard,'agent_panel_collapsed',False)
            if key==ord('j'):
                from tools.automation.ui import open_data_center
                open_data_center(dashboard)
            if key==ord('t'):
                from tools.automation.ui import open_patrol
                open_patrol(dashboard)
            if key==ord('a'):
                from tools.control_agent.ui import open_review
                open_review(dashboard)
            if key==ord('c'):dashboard.select_camera()
            if key==ord('y'):dashboard.change_telemetry_connection()
            if key==ord('b'):dashboard.show_observation_details=not getattr(dashboard,'show_observation_details',False)
            if key==ord('o'):dashboard.add_source(False)
            if key==ord('u'):dashboard.add_source(True)
            if key==ord('h'):c.show_fire_smoke=not c.show_fire_smoke
            if key==ord('k'):c.site_mode={'AUTO':'ON','ON':'OFF','OFF':'AUTO'}[c.site_mode]
            if key==ord('l'):c.show_direction_overlay=not c.show_direction_overlay
            if key==ord('i'):dashboard.show_auto_debug=not dashboard.show_auto_debug
            if args.seconds and time.monotonic()-start>=args.seconds:break
            time.sleep(.015)
    finally:
        last_observation=model.snapshot()
        if hasattr(source,'close'):source.close()
        model.clear()
        analyzer.stop_event.set()
        for c in channels+dashboard.retired:c.stop()
        analyzer.join()
        for c in channels+dashboard.retired:
            if c.reader is not None:c.reader.join(timeout=3)
        if dashboard.floating_zoom is not None:dashboard.floating_zoom.close()
        dashboard.main_view.close();bridge.close()
        evidence_bridge.sync_channels([])
        for camera in list(incident_agent.camera_sources):incident_agent.bind_camera(camera,incident_agent.camera_sources[camera],available=False)
        if getattr(dashboard,'agent_review_dialog',None) is not None:dashboard.agent_review_dialog.close()
        if flight_visual is not None:flight_visual.close()
        automation.close()
        result=dict(flight_visual=dict(captures=flight_captures,time_scale=flight_scale,states=flight_visual.states if flight_visual else []),automation=dict(data_center_status=automation.hub_status,flight=automation.flight.snapshot() if automation.flight else None,report_status=automation.reports.status,error=automation.error,sim_patrol=automation.patrol.snapshot()),incident_store=str(store_path),legacy_import=imported_from,store_error=store_error,incident_agent=incident_agent.snapshot(),agent_event_errors=evidence_bridge.errors,mode='OFFLINE_DEMO',frames=frames,current_risk=dashboard.current_risk,active_alerts=dashboard.hazard_journal.active,recent_hazards=list(dashboard.hazard_journal.history),channels=[dict(number=c.number,status=c.status,camera_bridge=getattr(c,'camera_bridge_metrics',None).snapshot() if hasattr(c,'camera_bridge_metrics') else None,camera_metadata=getattr(c,'camera_bridge_metadata',None),object_count=len(c.objects),source=c.source,received_sequence=c.sequence,received_at=c.received_at,reader_exit_confirmed=c.reader is None or not c.reader.is_alive(),decoder_cleanup=getattr(c,'decoder_cleanup',None),connection_error=getattr(c,'connection_error',None),analysis_completed=getattr(c,'analysis_completed_count',0),analysis_sequence=c.analysis_sequence,auto_track=c.auto_control.enabled) for c in channels if c.source],observation=model.snapshot(),last_observation=last_observation,telemetry_connection=source.connection_info() if hasattr(source,'connection_info') else None,telemetry_cleanup=getattr(source,'cleanup',None),data_origin=getattr(source,'origin','FIXTURE'),source_kind=getattr(source,'source_label','MEMORY FIXTURE'),analyzer_error=analyzer.error,layouts=sorted(captured),flight_commands_sent=0,actual_socket_creation_allowed=False,auto_track=False,auto_patrol=False)
        (out/'result.json').write_text(json.dumps(result,indent=2));incident_store.close();print(str(out))
    reader_failure=any(c.reader is not None and c.reader.is_alive() for c in channels+dashboard.retired)
    decoder_failure=any(d.get('forced_termination',False) or not all(d.get(k,False) for k in ('process_exited','stdout_closed','stderr_closed','threads_exited')) for c in channels+dashboard.retired if (d:=getattr(c,'decoder_cleanup',None)) is not None)
    return int(reader_failure or decoder_failure or bool(analyzer.error) or result['incident_agent']['audit_status']!='OK' or result['automation']['report_status']!='OK')

if __name__=='__main__':raise SystemExit(main())
