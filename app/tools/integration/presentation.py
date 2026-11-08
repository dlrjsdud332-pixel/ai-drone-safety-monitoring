"""Display-only control-room styling. No detector or security policy changes."""
import time
from pathlib import Path
from collections import deque
from tools.ai.smoke_motion import status_line as smoke_status_line


def smoke_display(channel):
    estimator=getattr(channel,'smoke_motion_estimator',None)
    return smoke_status_line(estimator.snapshot(time.monotonic()) if estimator else None)


def install_rendering(app):
    cv=app.cv2
    class DisplayCV:
        def __getattr__(self,name):return getattr(cv,name)
        def rectangle(self,image,a,b,color,thickness=1,*args,**kwargs):
            if color==(0,255,180) and thickness==2:color=(180,190,55);thickness=1
            if a[1]==5 and b[1]==35:
                color={(0,150,0):(110,112,30),(65,65,65):(55,43,32),(120,90,0):(105,90,30),(100,85,25):(70,53,35)}.get(color,color)
            return cv.rectangle(image,a,b,color,thickness,*args,**kwargs)
        def putText(self,image,text,*args,**kwargs):
            # Replace repeated per-frame red warning lines with one tile banner.
            if text in ('FIRE DETECTED','SMOKE DETECTED'):return image
            return cv.putText(image,text,*args,**kwargs)
    app.cv2=DisplayCV()
    app.SUPPRESS_INLINE_LOW_LIGHT=True
    app.SUPPRESS_INLINE_STATS=True
    app.COMPACT_COMPASS=True
    app.DEFAULT_FILL=True
    base=app.DetectionLabelLayout
    class Labels(base):
        def draw(self,image,reserved=()):
            # Deduplicate overlapping labels only; detector boxes/events remain intact.
            kept=[]
            for request in self.requests:
                duplicate=False
                if request.key.startswith(('FIRE:','SMOKE:')):
                    a=request.box
                    for other in kept:
                        if request.key.split(':')[0]!=other.key.split(':')[0]:continue
                        b=other.box;intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
                        area=min(max(1,(a[2]-a[0])*(a[3]-a[1])),max(1,(b[2]-b[0])*(b[3]-b[1])))
                        if intersection/area>=.65:duplicate=True;break
                if not duplicate:kept.append(request)
            self.requests=kept
            super().draw(image,reserved)
            for key,text,rect in self.placed:
                if not key.startswith(('FIRE:','SMOKE:')):continue
                x,y,right,bottom=rect
                cv.rectangle(image,(x,y),(right,bottom),(45,45,165) if key.startswith('FIRE:') else (35,110,185),-1)
                cv.putText(image,text,(x+3,bottom-4),cv.FONT_HERSHEY_SIMPLEX,.35,(255,255,255),1,cv.LINE_AA)
            return image
    app.DetectionLabelLayout=Labels


def text(cv,image,value,x,y,width,scale=.44,color=(225,233,240)):
    if any(ord(c)>127 for c in value):
        from PySide6.QtGui import QGuiApplication,QImage,QPainter,QFont,QColor,QFontMetrics
        if QGuiApplication.instance() is not None:
            font=QFont('Apple SD Gothic Neo');font.setPixelSize(max(12,round(28*scale/.65)))
            measured=QFontMetrics(font).horizontalAdvance(value)
            if measured>width:font.setPixelSize(max(10,int(font.pixelSize()*width/measured)))
            q=QImage(image.data,image.shape[1],image.shape[0],image.strides[0],QImage.Format_BGR888)
            painter=QPainter(q);painter.setFont(font);painter.setPen(QColor(*reversed(color)));painter.drawText(int(x),int(y),value);painter.end()
            return
        value='NO VIDEO / ADD SOURCE [O]'
    measured=cv.getTextSize(value,cv.FONT_HERSHEY_SIMPLEX,scale,1)[0][0]
    cv.putText(image,value,(int(x),int(y)),cv.FONT_HERSHEY_SIMPLEX,scale*min(1,width/max(1,measured)),color,1,cv.LINE_AA)


NAVY=(29,23,17)
CARD=(46,36,26)
TEAL=(192,205,74)
MUTED=(165,157,144)
FIRE=(62,64,205)
SMOKE=(35,143,233)


class HazardJournal:
    """Display state only; original detector/event files are never modified."""
    def __init__(self):
        self.history=deque(maxlen=100);self.previous={};self.active={}
    def update(self,channels,wall=None,monotonic=None):
        wall=time.time() if wall is None else wall
        monotonic=time.monotonic() if monotonic is None else monotonic
        active={};keys=set()
        for c in channels:
            with c.lock:
                key=(id(c),c.source,getattr(c,'source_generation',0));keys.add(key)
                current_generation=(getattr(c,'analysis_generation',None)==getattr(c,'source_generation',None))
                stopped=getattr(c,'stop_event',None) is not None and c.stop_event.is_set()
                live=not stopped and current_generation and bool(c.source) and c.status=='Playing' and 0<=monotonic-c.received_at<2 and 0<=monotonic-c.overlay_time<2
                flags=[]
                observed=not stopped and current_generation and bool(c.source) and c.status in ('Playing','Ended') and 0<=monotonic-c.overlay_time<2
                if observed:
                    for name,attribute in (('FIRE','fire_until'),('SMOKE','smoke_until'),('CROWD','crowd_until')):
                        if wall<getattr(c,attribute,0):flags.append(name)
                    if any(wall<v for v in getattr(c,'fall_until',{}).values()):flags.append('FALL')
                    if getattr(c,'previous_danger',False):flags.append('INTRUSION')
                    required=c.site_mode=='ON' or (c.site_mode=='AUTO' and c.danger is not None)
                    if required:
                        for name,attribute in (('NO HARDHAT','hardhat_until'),('NO VEST','vest_until')):
                            if wall<getattr(c,attribute,0):flags.append(name)
                current=set(flags)
                for kind in sorted(current-self.previous.get(key,set())):
                    self.history.append(dict(camera=c.number,kind=kind,at=wall,source=c.source,source_generation=getattr(c,'source_generation',0)))
                self.previous[key]=current;active[c.number]=flags if live else []
        self.previous={k:v for k,v in self.previous.items() if k in keys}
        self.active=active
        return active


def journal(dashboard):
    if not hasattr(dashboard,'hazard_journal'):dashboard.hazard_journal=HazardJournal()
    dashboard.hazard_journal.update(dashboard.channels)
    return dashboard.hazard_journal


def panel(app,dashboard,canvas,model,source):
    if dashboard.layout!=1:return
    dashboard.agent_hitboxes=[]
    if hasattr(dashboard,'evidence_bridge'):dashboard.evidence_bridge.sync_channels(dashboard.channels)
    cv=app.cv2;s=model.snapshot();d=s['data'] or {};p=d.get('position') or {};packet=source.packet if s['fresh'] else {};j=journal(dashboard)
    left=dashboard.video_right;x=left+8;width=app.WIDTH-left-16
    cv.rectangle(canvas,(left,app.HEADER),(app.WIDTH-1,app.HEIGHT-1),NAVY,-1)
    # Fit the existing 200px column without taking space from the video.
    available=app.HEIGHT-app.HEADER-app.FOOTER-16;unit=min(1.,available/(730 if hasattr(dashboard,'incident_agent') else 650));y=app.HEADER+8
    def card(title,lines,height):
        nonlocal y
        height=int(height*unit);cv.rectangle(canvas,(x,int(y)),(x+width,int(y+height)),CARD,-1)
        text(cv,canvas,title,x+9,y+18*unit,width-18,.34,MUTED)
        for i,(value,color,scale) in enumerate(lines):text(cv,canvas,value,x+9,y+(40+i*23)*unit,width-18,scale*unit,color)
        start=y;y+=height+8*unit;return start
    flight=dashboard.automation.flight.snapshot() if getattr(dashboard,'automation',None) is not None and dashboard.automation.flight is not None else None
    if flight is not None:
        f=flight['telemetry'] or {};s=dict(s,data=dict(position=dict(lat=f['latitude'],lon=f['longitude']) if f.get('latitude') is not None and f.get('longitude') is not None else {},battery=f.get('battery_pct')),telemetry=dict(relative_altitude_m=f.get('relative_altitude_m'),home_status=f.get('home')));d=s['data'];p=d['position'];packet=dict(flight_mode=f.get('mode','UNKNOWN'),mission_status=flight['state'],mission_progress=f.get('progress_pct'))
    live_rx=getattr(source,'is_readonly_px4',False)
    if getattr(source, "is_readonly_px4", False):
        from tools.control_ui.px4_readonly.source import source_status
    card('FLIGHT OBSERVATION',[('PX4 READ ONLY' if live_rx else 'OFFLINE DEMO',TEAL,.53),('COMMANDS BLOCKED' if live_rx else 'SIMULATED PX4' if flight else 'NO REAL DRONE',MUTED,.35)],74)
    card('SECURITY',[(s['gate'],FIRE if s['gate']=='BLOCKED' else TEAL,.52),(source_status(s) if live_rx else getattr(source,'source_label','MEMORY FIXTURE') if s['fresh'] else 'AUTH / DATA UNKNOWN' if s.get('channel_authenticated') else 'DATA UNKNOWN',MUTED,.34)],74)
    details=s.get('telemetry') or {};relative=details.get('relative_altitude_m');alt=relative if relative is not None else details.get('altitude_msl_m',p.get('alt_msl'))
    card('POSITION / GPS / HOME '+('ALT' if (s.get('telemetry') or {}).get('home_status') else '?'),[(f"LAT {p['lat']:.6f}" if 'lat' in p else 'LAT UNKNOWN',TEAL,.46),(f"LON {p['lon']:.6f}" if 'lon' in p else 'LON UNKNOWN',TEAL,.46),(('ALT REL ' if relative is not None else 'ALT MSL ')+(f'{alt:.1f} m' if alt is not None else 'UNKNOWN'),(235,240,245),.58)],105)
    battery=d.get('battery');start=card('BATTERY / MODE',[(f'{battery:.0f}%' if battery is not None else 'UNKNOWN',TEAL,.66),(packet.get('flight_mode','MODE UNKNOWN'),MUTED,.38)],104)
    gauge(cv,canvas,x+9,start+88*unit,width-18,battery,TEAL)
    if live_rx:
        age=s.get('age');info=details.get('field_states',{})
        card('ARM / GROUND / HOME',[(('ARM '+str(details.get('armed'))+' / LAND '+str(details.get('landed'))),TEAL,.38),('HOME '+str(info.get('home_status','NEVER_RECEIVED')),MUTED,.34),('HB '+(f'{age:.2f}s' if age is not None else 'NEVER')+' / '+('ACTUAL' if s['fresh'] else 'STALE / UNKNOWN'),MUTED,.32)],104)
    else:
        progress=packet.get('mission_progress');start=card(('WP '+str((flight['telemetry'] or {}).get('waypoint','UNKNOWN'))) if flight else 'MISSION / ITEM INDEX' if details else 'MISSION / FIXTURE',[(packet.get('mission_status','UNKNOWN'),TEAL,.46),(f'{progress:.1f}%' if progress is not None else 'UNKNOWN',TEAL,.6)],104)
    if not live_rx:gauge(cv,canvas,x+9,start+88*unit,width-18,progress,TEAL)
    if hasattr(dashboard,'incident_agent'):
        from tools.control_agent.ui import panel_rows
        agent=panel_rows(dashboard.incident_agent)
        review_top=card('AI REVIEW / RULE BASED',[(agent['severity']+'  '+str(agent['count'])+' CURRENT',FIRE if agent['severity']=='HIGH' else TEAL,.44),(agent['recommendation'],MUTED,.32),('HUMAN '+agent['review']+' / NO EXEC',TEAL,.31),(smoke_display(dashboard.channels[dashboard.selected]),TEAL,.29)],123)
        history_top=card('PAST / UNRESOLVED '+str(agent['held']),[(agent['recent'][0] if agent['recent'] else 'NO RECORDED INCIDENT',MUTED,.32),(agent['recent'][1] if len(agent['recent'])>1 else 'NO OTHER INCIDENT',MUTED,.31),('[A] 검토 / AUDIT '+agent['audit'],TEAL,.31)],90)
        dashboard.agent_hitbox_shape=(app.WIDTH,app.HEIGHT,dashboard.layout)
        dashboard.agent_hitboxes=[((x,review_top,x+width,review_top+123*unit),agent['urgent_id']),((x,history_top,x+width,history_top+90*unit),None)]
    else:
        active=sorted({kind for kinds in j.active.values() for kind in kinds})
        card('CURRENT RISK',[(' / '.join(active) if active else 'NO CURRENT ALERT',FIRE if 'FIRE' in active else SMOKE if active else TEAL,.39),(smoke_display(dashboard.channels[dashboard.selected]),TEAL,.29)],78)
        recent=list(j.history)[-1:]
        card('RECENT HISTORY',[(f"CAM {recent[0]['camera']}  {recent[0]['kind']}" if recent else 'NO RECORDED ALERT',MUTED,.37)],55)


def gauge(cv,image,x,y,width,value,color):
    x,y,width=int(x),int(y),int(width)
    cv.rectangle(image,(x,y),(x+width,y+5),(65,57,46),-1)
    if value is not None and isinstance(value,(int,float)) and 0<=value<=100:
        cv.rectangle(image,(x,y),(x+int(width*value/100),y+5),color,-1)
    # Missing data stays an empty rail, never a fabricated zero percent.


def overlays(app,dashboard,image,model,source):
    if hasattr(dashboard,'evidence_bridge'):dashboard.evidence_bridge.sync_channels(dashboard.channels)
    if getattr(source, "is_readonly_px4", False):
        from tools.control_ui.px4_readonly.source import source_status
    cv=app.cv2;s=model.snapshot();top=app.HEIGHT-app.FOOTER;j=journal(dashboard)
    cv.rectangle(image,(0,top),(app.WIDTH,app.HEIGHT),NAVY,-1)
    active=sorted({kind for flags in j.active.values() for kind in flags})
    text(cv,image,('PX4 RX ONLY | COMMANDS BLOCKED | ' if getattr(source,'is_readonly_px4',False) else 'OFFLINE | COMMANDS BLOCKED | ')+(' / '.join(active) if active else 'NO ALERT'),12,top+25,app.WIDTH-24,.48,TEAL)
    recent=list(j.history)[-3:]
    history='   '.join(time.strftime('%H:%M:%S',time.localtime(r['at']))+f" CAM{r['camera']} {r['kind']} [{Path(r.get('source') or 'UNKNOWN').name[:18]}]" for r in recent)
    text(cv,image,'DETAILS [B]  |  V: 1/2/4 VIEWS  |  '+smoke_display(dashboard.channels[dashboard.selected]),12,top+50,app.WIDTH-24,.40,MUTED)
    if dashboard.layout in (2,4) and hasattr(dashboard,'incident_agent'):
        from tools.control_agent.ui import compact
        compact(app,dashboard,image,top)
    cv.rectangle(image,(0,36),(app.WIDTH,app.HEADER-1),NAVY,-1)
    cv.rectangle(image,(app.WIDTH-145,40),(app.WIDTH-10,65),CARD,-1)
    text(cv,image,'WINDOW [F]' if dashboard.main_view and dashboard.main_view.is_fullscreen() else 'FULL [F]',app.WIDTH-136,58,120,.45,TEAL)
    text(cv,image,('PX4 RX / ' if getattr(source,'is_readonly_px4',False) else 'OFFLINE DEMO / SECURITY ')+s['gate']+' / '+(source_status(s) if getattr(source,'is_readonly_px4',False) else (getattr(source,'source_label','FIXTURE DATA')+' '+getattr(source,'origin','')) if s['fresh'] else 'DATA UNKNOWN'),10,57,max(20,app.WIDTH-330),.38,TEAL)
    display_button=(app.WIDTH-315,40,app.WIDTH-155,65)
    dashboard.display_button=display_button
    cv.rectangle(image,display_button[:2],display_button[2:],CARD,-1)
    text(cv,image,'FILL / FIT' if getattr(dashboard,'display_fill',True) else 'FIT / FILL',display_button[0]+9,58,142,.46,TEAL)
    visible=[dashboard.selected] if dashboard.layout==1 else list(range(dashboard.layout));cols,rows=dashboard.grid_shape(dashboard.layout)
    w=dashboard.video_right//cols;h=(app.HEIGHT-app.HEADER-app.FOOTER)//rows
    for index in visible:
        c=dashboard.channels[index];slot=0 if dashboard.layout==1 else index;left=(slot%cols)*w;tiletop=app.HEADER+(slot//cols)*h;bottom=tiletop+h
        with c.lock:ended=bool(c.source) and c.status=='Ended';empty=not c.source;status=c.status
        if empty:
            cv.rectangle(image,(left+2,tiletop+2),(left+w-3,bottom-29),NAVY,-1)
            text(cv,image,'영상 없음',left+20,tiletop+h//2-10,w-40,.65,TEAL)
            text(cv,image,'로컬 소스 추가 [O]',left+20,tiletop+h//2+20,w-40,.42,MUTED)
        flags=j.active.get(c.number,[]);fire='FIRE' in flags
        intrusion='INTRUSION' in flags
        hazards=[name for name in ('INTRUSION','FIRE','SMOKE') if name in flags]
        label='ENDED / LAST FRAME' if ended else ' + '.join(hazards)+' DETECTED' if hazards else ''
        if label:
            color=CARD if ended else FIRE if intrusion or fire else SMOKE
            cv.rectangle(image,(left+8,bottom-26),(left+w-8,bottom-5),color,-1)
            # Use actual cv2 to bypass the legacy-warning suppression adapter.
            import cv2
            text(cv2,image,label,left+16,bottom-10,w-32,.48,(245,248,250))
    if getattr(dashboard,'show_observation_details',False):
        selected=dashboard.channels[dashboard.selected]
        auto=getattr(dashboard,'automation',None)
        patrol=auto.patrol.snapshot() if auto else {}
        lines=['DETAILS [B TO CLOSE]  |  V: VIEWS  F: FULLSCREEN',
               'SOURCE: '+(Path(selected.source).name if selected.source and '://' not in selected.source else str(selected.source or 'NONE')),
               'LOW LIGHT: '+str(selected.night_mode)+' | SMOKE THR: '+f'{selected.smoke_conf_threshold:.2f}',
               ('VIDEO ERROR: '+str(selected.connection_error)) if getattr(selected,'connection_error',None) else 'SMOKE: '+smoke_display(selected),
               'SIM PATROL: '+str(patrol.get('state','UNKNOWN'))+' | TIME: '+str(patrol.get('elapsed','UNKNOWN')),
               'SIM POSITION: '+str(patrol.get('position','UNKNOWN'))+' | BAT: '+str(patrol.get('battery','UNKNOWN')),
               'RX/AI: '+f'{selected.receive_fps:.1f}'+' / '+f'{selected.analysis_rate.fps(time.monotonic()):.1f}']
        if getattr(source,'is_readonly_px4',False):
            detail=model.snapshot()['telemetry']
            lines=lines[:4]+['PX4 RX ONLY / OPERATING FRESHNESS UNKNOWN / COMMANDS BLOCKED',
                    'CONNECTION: '+detail.get('connection_state','UNKNOWN')+' | RX ERROR: '+str(model.snapshot().get('error') or 'NONE'),
                    ]
            from tools.control_ui.px4_readonly.source import home_lines
            lines+=home_lines(detail)
            lines+=['TARGET: '+str(detail.get('target_ids','UNKNOWN'))+' | CONNECTION GENERATION: '+str(detail.get('connection_generation','UNKNOWN'))]
            for name,state in detail['field_states'].items():
                age=detail['field_ages_s'][name]
                lines.append(name+': '+state+' | '+('NEVER' if age is None else f'{age:.2f}s')+' | '+str(detail['field_messages'][name]))
        x,y=20,app.HEADER+20;width=min(app.WIDTH-40,740);height=min(app.HEIGHT-app.HEADER-app.FOOTER-45,28+len(lines)*25)
        cv.rectangle(image,(x,y),(x+width,y+height),NAVY,-1)
        for i,line in enumerate(lines):
            baseline=y+25+i*25
            if baseline<y+height-5:text(cv,image,line,x+12,baseline,width-24,.46,TEAL)


    if hasattr(dashboard,'automation'):
        from tools.automation.ui import overlay
        overlay(app,dashboard,image)
    # Paint the shared footer last: legacy and compact panels cannot overwrite it.
    cv.rectangle(image,(0,top),(app.WIDTH,app.HEIGHT),NAVY,-1)
    active_text=' / '.join(active) if active else 'NO ALERT'
    if getattr(source,'is_readonly_px4',False):
        from tools.control_ui.px4_readonly.source import footer_lines
        for i,line in enumerate(footer_lines(model.snapshot(),active_text)):
            text(cv,image,line,12,top+20+i*23,max(100,app.WIDTH-(360 if i==2 else 190)),.44,TEAL if i==0 else MUTED)
    else:
        text(cv,image,'OFFLINE | COMMANDS BLOCKED | '+active_text,12,top+20,app.WIDTH-24,.48,TEAL)
        text(cv,image,getattr(dashboard,'shared_sim_summary','SIM UNKNOWN'),12,top+43,app.WIDTH-24,.43,MUTED)
        text(cv,image,'V: 1/2/4 VIEWS  |  F: FULLSCREEN',12,top+65,max(100,app.WIDTH-360),.40,MUTED)
    notice=getattr(dashboard,'source_notice','')
    if notice:
        cv.rectangle(image,(0,top+49),(app.WIDTH-185,top+72),NAVY,-1)
        text(cv,image,notice,12,top+66,max(100,app.WIDTH-360),.44,TEAL)
    if getattr(source,'is_readonly_px4',False):
        dashboard.telemetry_button=(app.WIDTH-172,top+5,app.WIDTH-12,top+35)
        cv.rectangle(image,dashboard.telemetry_button[:2],dashboard.telemetry_button[2:],CARD,-1)
        text(cv,image,'PX4 RX [Y]',app.WIDTH-163,top+27,142,.46,TEAL)
    dashboard.camera_button=(app.WIDTH-344,top+39,app.WIDTH-184,top+69)
    cv.rectangle(image,dashboard.camera_button[:2],dashboard.camera_button[2:],CARD,-1)
    text(cv,image,'LOCAL CAM [C]',app.WIDTH-335,top+60,142,.44,TEAL)
    button=(app.WIDTH-172,top+39,app.WIDTH-12,top+69)
    dashboard.details_button=button
    cv.rectangle(image,button[:2],button[2:],CARD,-1)
    text(cv,image,'CLOSE [B]' if getattr(dashboard,'show_observation_details',False) else 'DETAILS [B]',button[0]+9,top+60,142,.48,TEAL)


def draw_tile_information(app,dashboard,image,index):
    """One short tile label; detailed facts belong to the B panel."""
    cv=app.cv2;left,top,width,height=dashboard.tile_maps[index]
    channel=dashboard.channels[index]
    if not channel.source:return  # Empty tiles already display their own two-line prompt.
    playing_label='실시간 웹캠' if str(channel.source).isdecimal() else '실시간 Gazebo' if hasattr(channel,'camera_bridge_metrics') else '스트림 입력' if '://' in str(channel.source) else '녹화 영상'
    label=f'CAM {channel.number} | '+(playing_label if channel.status=='Playing' else channel.status)
    if hasattr(channel,'presentation_age_ms'):
        label+=f' | 분석영상 {channel.presentation_age_ms:.0f}ms'
        if getattr(channel,'presentation_stale',False):label+=' | 오래된 영상·결과'
    if channel.status=='Playing' and time.monotonic()-channel.received_at>2:label+=' | STALE / 오래된 영상'
    if channel.night_mode:label+=' | 저조도'
    if getattr(dashboard,'display_fill',True):label+=' | 확대·일부 잘림'
    if getattr(channel,'show_direction_overlay',True):
        snapshot=channel.smoke_motion_estimator.snapshot(time.monotonic())
        if snapshot and not snapshot.get('geographic_verified'):label+=' | 방위 미확인'
    margin=max(8,min(14,width//70));row=max(23,min(30,height//12))
    right=min(image.shape[1]-3,left+width-3)
    if right-left<130:return
    cv.rectangle(image,(left+3,top+3),(right,top+row+3),NAVY,-1)
    text(cv,image,label,left+margin,top+row-5,right-left-2*margin,.50,TEAL)
