"""Experimental image-space crowd regions. Not imported by the operational UI.

No physical density/metric distance claim. Thresholds are uncalibrated candidates.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CandidateConfig:
    radius_in_body_heights: float = 1.5
    max_radius_diagonal: float = .15
    min_people: int = 3
    high_people: int = 6
    caution_seconds: float = 1.5
    high_seconds: float = 3.
    clear_seconds: float = 2.
    continuity_gap_seconds: float = .5


RANK={'NORMAL':0,'CAUTION':1,'HIGH':2}


def overlap(a,b):
    inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return inter/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)


class CrowdRegions:
    def __init__(self,config=None):
        self.config=config or CandidateConfig();self.states={};self.next_id=1;self.last_time=None

    def update(self,tracks,now,width,height,zone=None):
        if not math.isfinite(now) or self.last_time is not None and now<self.last_time:
            raise ValueError('Expected nondecreasing finite timestamps')
        self.last_time=now;cfg=self.config;unique={};invalid=0;duplicates=0
        for person in tracks:
            if person.get('class_id',0)!=0:continue  # Caller may provide mixed tracked classes.
            tid=person.get('id');box=person.get('xyxy',[])
            if tid is None or len(box)!=4 or not all(math.isfinite(v) for v in box) or box[2]<=box[0] or box[3]<=box[1]:
                invalid+=1;continue
            if tid in unique:
                duplicates+=1
                if person.get('confidence',0)<=unique[tid].get('confidence',0):continue
            unique[tid]=person
        people=list(unique.values());n=len(people)
        feet=[((p['xyxy'][0]+p['xyxy'][2])/2,p['xyxy'][3]) for p in people]
        heights=[p['xyxy'][3]-p['xyxy'][1] for p in people];neighbors=[]
        for i in range(n):
            adjacent=[]
            for j in range(n):
                distance=math.dist(feet[i],feet[j])
                radius=min((heights[i]+heights[j])/2*cfg.radius_in_body_heights,math.hypot(width,height)*cfg.max_radius_diagonal)
                if distance<=radius:adjacent.append(j)
            neighbors.append(adjacent)
        # DBSCAN-style core connectivity; isolated people never form a region.
        cores={i for i in range(n) if len(neighbors[i])>=cfg.min_people};unvisited=set(cores);clusters=[]
        while unvisited:
            first=min(unvisited);queue=[first];core_group=set()
            while queue:
                i=queue.pop()
                if i not in unvisited:continue
                unvisited.remove(i);core_group.add(i);queue.extend(j for j in neighbors[i] if j in unvisited)
            clusters.append(set(core_group))
        assigned=set().union(*clusters) if clusters else set()
        for i in range(n):
            if i in assigned:continue
            choices=[k for k,c in enumerate(clusters) if any(j in cores and i in neighbors[j] for j in c)]
            if choices:clusters[choices[0]].add(i)
        observed=[]
        for indices in clusters:
            ids={people[i]['id'] for i in indices};boxes=[people[i]['xyxy'] for i in indices]
            bounds=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
            inside=sum(zone is not None and zone[0]<=feet[i][0]<=zone[2] and zone[1]<=feet[i][1]<=zone[3] for i in indices)
            raw='HIGH' if len(ids)>=cfg.high_people or inside>=cfg.min_people else 'CAUTION'
            observed.append(dict(ids=ids,bounds=bounds,raw=raw,inside=inside))
        changes=[];matched=set()
        for region in observed:
            candidates=[]
            for gid,state in self.states.items():
                if gid in matched or now-state['last_seen']>cfg.clear_seconds:continue
                shared=len(region['ids']&state['ids'])/max(1,len(region['ids']|state['ids']))
                geometry=overlap(region['bounds'],state['bounds'])
                if shared>=.3 or geometry>=.3:candidates.append((shared+geometry,-gid,gid))
            gid=max(candidates)[2] if candidates else self.next_id
            if not candidates:
                self.next_id+=1;self.states[gid]=dict(ids=set(),bounds=region['bounds'],first_seen=now,last_seen=now,raw=region['raw'],raw_since=now,level='NORMAL',lower_since=None,inside=0)
            state=self.states[gid];matched.add(gid)
            if now-state['last_seen']>cfg.continuity_gap_seconds:
                state['first_seen']=now;state['raw_since']=now
            if region['raw']!=state['raw']:state['raw_since']=now
            state.update(ids=region['ids'],bounds=region['bounds'],raw=region['raw'],last_seen=now,inside=region['inside'])
            proposed='NORMAL'
            if now-state['first_seen']>=cfg.caution_seconds:proposed='CAUTION'
            if state['raw']=='HIGH' and now-state['raw_since']>=cfg.high_seconds:proposed='HIGH'
            old=state['level']
            if RANK[proposed]>=RANK[old]:state['level']=proposed;state['lower_since']=None
            else:
                if state['lower_since'] is None:state['lower_since']=now
                if now-state['lower_since']>=cfg.clear_seconds:state['level']=proposed;state['lower_since']=None
            if old!=state['level']:changes.append(dict(group_id=gid,old=old,new=state['level']))
        for gid in list(self.states):
            state=self.states[gid]
            if gid not in matched and (state['level']=='NORMAL' or now-state['last_seen']>=cfg.clear_seconds):
                if state['level']!='NORMAL':changes.append(dict(group_id=gid,old=state['level'],new='NORMAL'))
                del self.states[gid]
        groups=[dict(group_id=gid,level=s['level'],people=len(s['ids']),ids=sorted(s['ids']),xyxy=s['bounds'],inside_zone=s['inside'],stale=gid not in matched,observed_seconds=now-s['first_seen']) for gid,s in sorted(self.states.items())]
        return dict(level=max((g['level'] for g in groups),key=lambda x:RANK[x],default='NORMAL'),
            unique_people=n,duplicate_ids_ignored=duplicates,invalid_tracks_ignored=invalid,groups=groups,transitions=changes,
            measurement='IMAGE-SPACE / UNCALIBRATED',physical_density=None)
