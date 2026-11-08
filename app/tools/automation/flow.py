"""Experimental image-space optical flow. Not geographic bearing or wind."""
import time
import cv2
import numpy as np

class SmokeFlow:
    def __init__(self):self.previous={};self.values={}
    def clear(self,camera):self.previous.pop(camera,None);self.values.pop(camera,None)
    def get(self,camera,now=None):
        v=self.values.get(str(camera));now=time.time() if now is None else now
        return dict(v) if v and v.get('at') is not None and 0<=now-v['at']<3 else self.unknown()
    @staticmethod
    def unknown():return dict(direction='UNKNOWN',label='EXPERIMENTAL / IMAGE ONLY',at=None,method='BACKGROUND_COMPENSATED_OPTICAL_FLOW',geographic_bearing='UNKNOWN',wind='UNKNOWN')
    def update(self,camera,source,frame,box,at):
        camera=str(camera);gray=cv2.cvtColor(cv2.resize(frame,(320,180)),cv2.COLOR_BGR2GRAY)
        old=self.previous.get(camera);self.previous[camera]=(source,at,gray)
        result=self.unknown()
        if old is None or old[0]!=source or not 0<at-old[1]<=1 or box is None:self.values[camera]=result;return result
        h,w=frame.shape[:2];x1,y1,x2,y2=box;x1,x2=[max(0,min(320,int(x*320/w))) for x in (x1,x2)];y1,y2=[max(0,min(180,int(y*180/h))) for y in (y1,y2)]
        mask=np.zeros_like(gray);mask[y1:y2,x1:x2]=255
        if np.count_nonzero(mask)<100 or np.count_nonzero(mask)>gray.size*.65:self.values[camera]=result;return result
        # Estimate global camera affine motion from features OUTSIDE smoke.
        pts=cv2.goodFeaturesToTrack(old[2],200,.02,8,mask=255-mask)
        if pts is None or len(pts)<12:self.values[camera]=result;return result
        nxt,status,_=cv2.calcOpticalFlowPyrLK(old[2],gray,pts,None)
        if nxt is None:self.values[camera]=result;return result
        ok=status.ravel()==1;a=pts[ok];b=nxt[ok]
        if len(a)<12:self.values[camera]=result;return result
        affine,inliers=cv2.estimateAffinePartial2D(a,b,method=cv2.RANSAC,ransacReprojThreshold=1.5)
        if affine is None or inliers.mean()<.6:self.values[camera]=result;return result
        aligned=cv2.warpAffine(old[2],affine,(320,180));flow=cv2.calcOpticalFlowFarneback(aligned,gray,None,.5,3,15,3,5,1.2,0)
        samples=flow[mask>0];v=np.median(samples,axis=0);mag=float(np.linalg.norm(v));coherent=np.mean(np.linalg.norm(samples-v,axis=1)<max(1.,mag))
        if not np.isfinite(v).all() or mag<.4 or coherent<.6:self.values[camera]=result;return result
        dx,dy=map(float,v);direction=('RIGHT' if dx>0 else 'LEFT') if abs(dx)>abs(dy)*1.5 else ('DOWN' if dy>0 else 'UP') if abs(dy)>abs(dx)*1.5 else ('RIGHT' if dx>0 else 'LEFT')+'_'+('DOWN' if dy>0 else 'UP')
        result.update(direction=direction,at=at,residual_px=[dx,dy],background_inlier_ratio=float(inliers.mean()),coherence=float(coherent));self.values[camera]=result;return result
