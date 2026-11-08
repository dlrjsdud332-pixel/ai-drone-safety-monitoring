"""Pure virtual waypoint clock; coordinates are explicitly SIMULATED, never GPS."""
import copy
import math
import time

class SimPatrol:
    states=('IDLE','READY','PATROLLING','PAUSED','RETURNING','COMPLETED','ABORTED')
    def __init__(self,record=lambda row:None,clock=time.monotonic,wallclock=time.time):
        self.wallclock=wallclock;self.observation_enabled=False;self.observation={};self.bound_sources=None;self.held_events={};self.handled_events=set();self.source_changed=False;self.session_started_wall=None
        self.record,self.clock=record,clock;self.state='IDLE';self.route=[];self.elapsed=0.;self.battery=None;self.position=None;self.index=0;self.progress=0.;self.last=None;self.period=1.;self.reason='';self.cycle=0
    def snapshot(self):
        return dict(label='SIMULATED / OFFLINE_DEMO',state=self.state,position=self.position,battery=self.battery,progress=self.progress,elapsed=self.elapsed,waypoint=self.index,period=self.period,reason=self.reason,cycle=self.cycle,session=self.cycle,observation=copy.deepcopy(self.observation),real_auto_patrol=False,flight_commands=0,execution_ready=False,flight_authorized=False)
    def _change(self,state,reason):
        if state in ('ABORTED','COMPLETED') and self.observation:
            self.observation.update(state='INACTIVE',reason=reason,valid_until=0.)
        row=dict(self.snapshot(),state=state,reason=reason,at=time.time())
        try:self.record(row)
        except Exception:
            self.state='ABORTED';self.reason='HISTORY_WRITE_FAILED';self.position=None
            if self.observation:self.observation.update(state='INACTIVE',reason=self.reason,valid_until=0.)
            raise
        self.state,self.reason=state,reason
    def configure(self,route,period=10.,battery=100.):
        if self.state in ('PATROLLING','RETURNING','PAUSED'):raise ValueError('stop simulation before changing route')
        if not isinstance(route,list) or not 2<=len(route)<=100:raise ValueError('2..100 waypoints required')
        if type(period) not in (int,float) or not math.isfinite(period) or not 1<=period<=3600:raise ValueError('period 1..3600 seconds per segment')
        if type(battery) not in (int,float) or not math.isfinite(battery) or not 0<=battery<=100:raise ValueError('virtual battery range')
        for p in route:
            if not isinstance(p,(list,tuple)) or len(p)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) or abs(x)>10000 for x in p) or p[2]<0:raise ValueError('local x,y,height metres required')
        self.route=[list(p) for p in route];self.period=float(period);self.battery=float(battery);self.position=list(route[0]);self.elapsed=0.;self.index=0;self.progress=0.;self.cycle+=1;self.session_started_wall=self.wallclock();self.bound_sources=None;self.held_events={};self.handled_events=set();self.source_changed=False;self.observation=dict(state='WAITING_INPUTS',risk='UNKNOWN',events=[],valid_until=0.);self._change('READY','USER_CONFIGURED_VIRTUAL_ROUTE')
    def start(self):
        if self.state not in ('READY','PAUSED'):raise ValueError('simulation not ready')
        if self.position is None or self.battery<=20:raise ValueError('virtual position/battery unavailable')
        if self.observation_enabled and self.state=='READY' and (self.observation.get('state')!='MONITORING' or not self.clock()<self.observation.get('valid_until',0)):raise ValueError('SIM_INPUT_UNVERIFIED')
        if self.observation_enabled and self.state=='PAUSED':
            if self.observation.get('state')!='RESUME_REQUESTED' or not self.clock()<self.observation.get('valid_until',0):raise ValueError('SIM_RESUME_REQUIRES_FRESH_REQUEST')
            self.handled_events.update(self.held_events);self.held_events={};self.observation['state']='MONITORING'
        self.last=self.clock();self._change('PATROLLING','USER_SIM_START')
    def pause(self):
        if self.state!='PATROLLING':raise ValueError('not patrolling')
        self.tick()
        if self.state=='PATROLLING':self._change('PAUSED','USER_SIM_PAUSE')
    def abort(self):self._change('ABORTED','USER_SIM_ABORT');self.last=None
    def finish(self):
        if self.state not in ('PATROLLING','PAUSED','RETURNING'):raise ValueError('no active simulation')
        self._change('COMPLETED','USER_SIM_END_NOT_ROUTE_COMPLETION');self.last=None
    def lose_position(self):
        self.position=None;self._change('ABORTED','SIM_POSITION_LOST');self.last=None
    def observe(self,session,events,sources,valid_until,event_ttl=3.):
        """Current local SIM session only. Never a physical risk-clearance authority."""
        if type(session) is not int or session!=self.cycle or self.state not in ('READY','PATROLLING','PAUSED','RETURNING'):return False
        self.observation_enabled=True;now=self.clock()
        previous=self.observation.get('state');previous_valid_until=self.observation.get('valid_until',0.)
        if self.bound_sources is not None and sources!=self.bound_sources:self.source_changed=True
        healthy=bool(sources) and all(v[-1] for v in sources.values()) and math.isfinite(valid_until) and now<valid_until and not self.source_changed
        if healthy and self.bound_sources is None:self.bound_sources=copy.deepcopy(sources)
        current={e['id']:e for e in events if e.get('state')=='CURRENT' and e.get('camera_id') in sources and e.get('source_id')==sources[e['camera_id']][-1] and e.get('last_seen',-1)>self.session_started_wall}
        for key,e in current.items():
            if key not in self.handled_events and e.get('kind') in ('PERSON','FIRE','SMOKE','DANGER ZONE','FALL','NO-Hardhat','NO-Safety Vest'):self.held_events[key]=copy.deepcopy(e)
        if self.held_events:
            valid_until=min([valid_until]+[now+event_ttl-(self.wallclock()-e['last_seen']) for key,e in current.items() if key in self.held_events])
            healthy=healthy and now<valid_until
        if not healthy:state='UNKNOWN';reason='SOURCE_CHANGED_NEW_SESSION_REQUIRED' if self.source_changed else 'VIDEO_OR_TELEMETRY_UNKNOWN'
        elif any(key not in current for key in self.held_events):state='UNKNOWN';reason='OBSERVATION_LOST_NOT_RISK_CLEAR'
        elif any(e['kind']!='PERSON' for e in self.held_events.values()):state='HOLD_RISK';reason='RISK_OBSERVED_OR_CLEARANCE_UNVERIFIED'
        elif self.held_events:
            reviewed=all(e.get('review')=='APPROVED' and e.get('reviews') and e['reviews'][-1]['at']>=self.session_started_wall for e in self.held_events.values())
            state='ACKNOWLEDGED' if reviewed else 'WAITING_CONFIRMATION';reason='ACK_IS_NOT_RISK_CLEAR' if reviewed else 'USER_REVIEW_REQUIRED'
        else:state='MONITORING';reason='NO_NEW_EVENT_OBSERVED_NOT_RISK_CLEAR'
        # A pending request survives only while its exact current evidence is valid.
        if previous=='RESUME_REQUESTED' and now<previous_valid_until and state in ('ACKNOWLEDGED','MONITORING') and set(self.held_events)==getattr(self,'resume_events',set()):state=previous
        self.observation=dict(state=state,reason=reason,risk='OBSERVED_RISK' if any(e['kind']!='PERSON' for e in self.held_events.values()) else 'PERSON_INFO' if self.held_events else 'UNKNOWN',events=[dict(id=e['id'],kind=e['kind'],camera=e['camera_id'],source=e['source_id'],observed_at=e['last_seen'],review=e['review']) for e in self.held_events.values()],valid_until=valid_until if healthy else 0.,session=session,risk_resolved=False)
        if state not in ('MONITORING','RESUME_REQUESTED') and self.state in ('PATROLLING','RETURNING'):
            self._change('PAUSED',reason);self.last=None
        return True
    def request_resume(self,session):
        if type(session) is not int or session!=self.cycle or self.state!='PAUSED':raise ValueError('SIM_SESSION_OR_STATE_MISMATCH')
        if self.observation.get('state') not in ('ACKNOWLEDGED','MONITORING') or not self.clock()<self.observation.get('valid_until',0):raise ValueError('SIM_RESUME_BLOCKED_RISK_OR_UNKNOWN')
        self.resume_events=set(self.held_events);self.observation['state']='RESUME_REQUESTED';self._change('PAUSED','USER_SIM_RESUME_REQUEST_NOT_EXECUTED')
    def tick(self):
        if self.state not in ('PATROLLING','RETURNING'):return
        now=self.clock();dt=now-self.last;self.last=now
        if not math.isfinite(dt) or dt<0 or dt>30:self._change('ABORTED','SIM_CLOCK_GAP');self.position=None;return
        if self.position is None:self.lose_position();return
        self.battery=max(0.,self.battery-dt*.1);self.elapsed+=dt
        if self.battery<=5:self._change('ABORTED','SIM_BATTERY_EXHAUSTED');return
        if self.battery<=20 and self.state=='PATROLLING':
            self.return_start=list(self.position);self.return_elapsed=0.;self._change('RETURNING','SIM_LOW_BATTERY_VIRTUAL_RETURN')
        if self.state=='RETURNING':
            self.return_elapsed+=dt;t=min(1.,self.return_elapsed/self.period);self.position=[a+(b-a)*t for a,b in zip(self.return_start,self.route[0])]
            if t>=1:self._change('COMPLETED','SIM_RETURN_COMPLETE')
        else:
            n=len(self.route)-1;f=min(float(n),self.elapsed/self.period);self.index=min(n-1,int(f));t=f-self.index
            self.position=[a+(b-a)*t for a,b in zip(self.route[self.index],self.route[self.index+1])];self.progress=f/n*100
            if f>=n:self._change('COMPLETED','SIM_ROUTE_COMPLETE')
