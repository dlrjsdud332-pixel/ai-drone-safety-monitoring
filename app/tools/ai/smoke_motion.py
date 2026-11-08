"""연속 영상의 배경 보정 연기 영역 이동. 지리 방향·바람 추정이 아니다."""
from collections import deque
import hashlib
import math
import cv2
import numpy as np
from .smoke_bearing import geographic_motion


def unknown(reason='NO_EVIDENCE'):
    return dict(direction='UNKNOWN',horizontal='UNKNOWN',vertical='UNKNOWN',
                dx_px_s=None,dy_px_s=None,reason=reason,basis='SCREEN',verified=False)


def status_line(result, ascii_only=False):
    if ascii_only:
        return 'SMOKE: '+(result['bearing_label']+' (NOT WIND)' if result and result.get('geographic_verified') else 'ANALYZING / BEARING UNVERIFIED')
    if result and result.get('geographic_verified'):
        return '연기 이동 방향: '+result['bearing_label']+' · 풍향 아님'
    return '연기 방향: 분석 중 · 방위 확인 불가'


def label(cv,image,text,point,color,size=10):
    """실제 Qt 창에서는 한국어, 비GUI 회귀에서는 ASCII 대체 문구를 쓴다."""
    try:
        from PySide6.QtGui import QGuiApplication,QImage,QPainter,QFont,QColor
        if QGuiApplication.instance() is not None:
            q=QImage(image.data,image.shape[1],image.shape[0],image.strides[0],QImage.Format_BGR888)
            painter=QPainter(q);font=QFont('Apple SD Gothic Neo');font.setPixelSize(size)
            painter.setFont(font);painter.setPen(QColor(*reversed(color)));painter.drawText(*point,text);painter.end();return
    except ImportError:pass
    fallback={'방위 확인 불가':'BEARING UNVERIFIED','연기 방향: 분석 중':'SMOKE: ANALYZING'}.get(text,text)
    cv.putText(image,fallback,point,cv.FONT_HERSHEY_SIMPLEX,.28,color,1,cv.LINE_AA)


class SmokeMotion:
    """2FPS 미만·빈 검출·중복·불안정 flow는 방향을 유지하지 않고 UNKNOWN."""
    def __init__(self):
        self.previous=None;self.history=deque(maxlen=4);self.result=unknown();self.updated=None;self.candidate_since=None

    def snapshot(self, now, geography=None):
        if self.updated is None or not 0<=now-self.updated<=.8:
            result=unknown('STALE');result['last_reason']=self.result['reason']
            result['last_quality']=self.result.get('quality',{})
        else:result=dict(self.result)
        if result.get('verified') and result.get('horizontal')=='SPREAD':
            result.update(geographic_motion(None,None));result['geographic_reason']='확산은 단일 수평 이동 방위가 아님'
        elif result.get('verified') and geography is not None:
            result.update(geographic_motion(geography.get('pixel'),(result['dx_px_s'],result['dy_px_s']),geography))
        else:result.update(geographic_motion(None,None))
        return result

    def update(self, frame, boxes, now, excluded=()):
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        scale=min(1.,320/gray.shape[1]);gray=cv2.resize(gray,None,fx=scale,fy=scale)
        signature=hashlib.blake2b(gray.tobytes(),digest_size=8).digest()
        def mask(rects):
            m=np.zeros(gray.shape,np.uint8)
            for rect in rects:
                x,y,u,v=np.rint(np.array(rect)*scale).astype(int)
                x,u=max(0,x),min(m.shape[1],u);y,v=max(0,y),min(m.shape[0],v)
                if x<u and y<v:m[y:v,x:u]=255
            return m
        current=(gray,mask(boxes),mask(list(boxes)+list(excluded)),now,signature)
        previous=self.previous;self.previous=current;self.updated=now
        self.result=unknown('UPDATING')
        quality=dict(smoke_pixels=int(np.count_nonzero(current[1])),background_attempted=False)
        def reject(reason):
            self.history.clear();self.candidate_since=None;self.result=dict(unknown(reason),quality=dict(quality));return self.result
        if previous is None:return reject('WARMUP')
        old,old_smoke,old_excluded,then,key=previous
        dt=now-then
        union=np.count_nonzero((old_smoke>0)|(current[1]>0)) if gray.shape==old.shape else 0
        quality.update(dt_sec=dt,duplicate=signature==key,smoke_region_iou=float(np.count_nonzero((old_smoke>0)&(current[1]>0))/union) if union else None)
        if gray.shape!=old.shape:return reject('RESOLUTION_CHANGE')
        if not 0<dt<=.5:return reject('LOW_FPS_OR_CLOCK')
        if signature==key:return reject('DUPLICATE_FRAME')
        if not boxes or not old_smoke.any():return reject('NO_SMOKE')
        if quality['smoke_region_iou']<.25:return reject('SMOKE_REGION_CHANGED')
        def tracks(region):
            points=cv2.goodFeaturesToTrack(old,180,.015,4,mask=region)
            if points is None:return None
            end,ok,error=cv2.calcOpticalFlowPyrLK(old,gray,points,None,winSize=(17,17),maxLevel=2)
            if end is None:return None
            back,valid,_=cv2.calcOpticalFlowPyrLK(gray,old,end,None,winSize=(17,17),maxLevel=2)
            if back is None:return None
            good=ok.ravel().astype(bool)&valid.ravel().astype(bool)&(np.linalg.norm(back-points,axis=2).ravel()<.8)
            good&=np.isfinite(end).all(axis=(1,2))
            return points.reshape(-1,2)[good],end.reshape(-1,2)[good]
        background=255-cv2.dilate(old_excluded,np.ones((9,9),np.uint8))
        quality['background_attempted']=True
        bg=tracks(background)
        quality['background_features']=0 if bg is None else len(bg[0])
        if bg is None or len(bg[0])<20:return reject('BACKGROUND_UNVERIFIED')
        affine,inliers=cv2.estimateAffinePartial2D(*bg,method=cv2.RANSAC,ransacReprojThreshold=1.)
        if affine is None or inliers is None or inliers.mean()<.7:return reject('BACKGROUND_UNSTABLE')
        predicted=cv2.transform(bg[0][None],affine)[0]
        noise=np.median(np.linalg.norm(bg[1][inliers.ravel()>0]-predicted[inliers.ravel()>0],axis=1))/scale
        quality['background_noise_px']=float(noise)
        if noise>.6:return reject('BACKGROUND_UNSTABLE')
        smoke=tracks(cv2.erode(old_smoke,np.ones((5,5),np.uint8)))
        quality['smoke_features']=0 if smoke is None else len(smoke[0])
        if smoke is None or len(smoke[0])<12:return reject('SMOKE_FEATURES_INSUFFICIENT')
        # Only features still within the detected plume can support a vector.
        end=np.rint(smoke[1]).astype(int)
        inside=(end[:,0]>=0)&(end[:,0]<gray.shape[1])&(end[:,1]>=0)&(end[:,1]<gray.shape[0])
        keep=np.where(inside)[0];keep=keep[current[1][end[keep,1],end[keep,0]]>0]
        if len(keep)<12:return reject('SMOKE_FEATURES_INSUFFICIENT')
        start,finish=smoke[0][keep],smoke[1][keep]
        residual=(finish-cv2.transform(start[None],affine)[0])/scale
        dx,dy=np.median(residual,axis=0);mag=float(np.hypot(dx,dy))
        quality['residual_median_px']=[float(dx),float(dy)]
        threshold=max(.35,float(noise)*3)
        mid=np.median(start[:,0]);left=residual[start[:,0]<mid];right=residual[start[:,0]>=mid]
        spreading=(len(left)>=5 and len(right)>=5 and np.median(left[:,0])<-threshold and np.median(right[:,0])>threshold)
        if mag<threshold and not spreading:return reject('MOTION_TOO_SMALL')
        consistency=float(np.mean(residual@np.array([dx,dy])>mag*mag*.25))
        quality['flow_consistency']=consistency
        if consistency<.65 and not spreading:return reject('SMOKE_FLOW_UNSTABLE')
        component_limit=max(threshold*.55,mag*.35)
        horizontal='RIGHT' if dx>component_limit else 'LEFT' if dx<-component_limit else 'NONE'
        vertical='UP' if dy<-component_limit else 'DOWN' if dy>component_limit else 'NONE'
        if spreading:
            horizontal='SPREAD'
        direction='_'.join(x for x in (horizontal,vertical) if x!='NONE') or 'UNKNOWN'
        if not self.history or self.history[-1]!=direction:self.candidate_since=now
        self.history.append(direction)
        if len(self.history)<3 or len(set(self.history))!=1 or now-self.candidate_since<.6-1e-6:
            self.result=dict(unknown('DIRECTION_UNSTABLE'),quality=dict(quality));return self.result
        self.result=dict(direction=direction,horizontal=horizontal,vertical=vertical,
            dx_px_s=float(dx/dt),dy_px_s=float(dy/dt),reason='BACKGROUND_COMPENSATED',
            basis='SCREEN',verified=True,background_noise_px=float(noise),feature_count=len(keep),fps=1/dt,quality=dict(quality))
        return dict(self.result)


def draw_compass(image, result, cv=cv2, side='right', compact=False):
    """방위 미검증 상태의 N/E 눈금은 참고 디자인이며 화살표를 그리지 않는다."""
    h,w=image.shape[:2]
    if w<240 or h<160:return None
    cx,cy=(42 if side=='left' else w-42),h-110;radius=24;color=(192,205,74)
    cv.circle(image,(cx,cy),radius,color,1,cv.LINE_AA)
    for angle in range(0,360,45):
        a=math.radians(angle);cv.line(image,(round(cx+20*math.sin(a)),round(cy-20*math.cos(a))),
            (round(cx+radius*math.sin(a)),round(cy-radius*math.cos(a))),color,1,cv.LINE_AA)
    for text,point in [('N',(cx-3,cy-29)),('S',(cx-3,cy+34)),('W',(cx-35,cy+3)),('E',(cx+29,cy+3))]:
        cv.putText(image,text,point,cv.FONT_HERSHEY_SIMPLEX,.25,color,1,cv.LINE_AA)
    valid=result.get('geographic_verified') and result.get('bearing_deg') is not None
    if valid:
        a=math.radians(result['bearing_deg'])
        cv.arrowedLine(image,(cx,cy),(round(cx+19*math.sin(a)),round(cy-19*math.cos(a))),(0,165,255),2,cv.LINE_AA,tipLength=.3)
    left=10 if side=='left' else max(2,w-170)
    label(cv,image,result['bearing_label']+' · 연기 이동' if valid else '방위 확인 불가',(left,h-67),color)
    if not compact:label(cv,image,'풍향 아님' if valid else '연기 방향: 분석 중',(left,h-50),color)
    return (0 if side=='left' else max(0,w-190),max(0,cy-radius-4),min(200,w) if side=='left' else w,h-40)
