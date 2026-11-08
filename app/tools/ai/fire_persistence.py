"""Bounded wall-clock FIRE confirmation; raw model evidence is mandatory."""
from .hazard_fusion import iou


class FirePersistence:
    def __init__(self, window=4., gap=2.5):
        self.window, self.gap = window, gap
        self.history = []
        self.last_time = None
        self.last_sequence = -1
        self.last_key = None

    def update(self, sequence, now, boxes, key, scene_stable):
        if (self.last_time is not None and
            (now <= self.last_time or now-self.last_time > self.gap or sequence <= self.last_sequence)) or not scene_stable:
            self.history.clear()
        self.history = [(t,b,k) for t,b,k in self.history if 0 < now-t <= self.window]
        confirmed = []
        if sequence > self.last_sequence and key != self.last_key:
            for box in boxes:
                if any(k != key and now-t >= .3 and iou(box,b) >= .3 for t,b,k in self.history):
                    confirmed.append(box)
            self.history.extend((now,box,key) for box in boxes)
        self.last_time, self.last_sequence, self.last_key = now, sequence, key
        return confirmed


class SceneMotionGuard:
    """Require measurable stationary background; unknown/moving scenes reset memory."""
    def __init__(self):
        self.previous = None
        self.median_flow = None

    def update(self, frame, excluded):
        import cv2
        import numpy as np
        gray=cv2.resize(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(320,240))
        mask=np.full(gray.shape,255,np.uint8)
        sx,sy=320/frame.shape[1],240/frame.shape[0]
        for x,y,u,v in excluded:
            cv2.rectangle(mask,(int(x*sx),int(y*sy)),(int(u*sx),int(v*sy)),0,-1)
        prior=self.previous;self.previous=gray;self.median_flow=None
        if prior is None:return False
        features=cv2.goodFeaturesToTrack(prior,maxCorners=150,qualityLevel=.01,minDistance=5,mask=mask)
        if features is None or len(features)<20:return False
        points,status,error=cv2.calcOpticalFlowPyrLK(prior,gray,features,None)
        if points is None:return False
        good=status.ravel().astype(bool)&(error.ravel()<20)
        if good.sum()<20:return False
        self.median_flow=float(np.median(np.linalg.norm((points-features).reshape(-1,2)[good],axis=1)))
        return self.median_flow <= 1.
