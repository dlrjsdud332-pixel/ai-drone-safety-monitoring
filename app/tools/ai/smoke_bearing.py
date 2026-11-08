"""깊이·수직속도·동기 자세가 검증된 경우에만 ENU 연기 이동 방위를 계산한다."""
import math
import numpy as np


def geographic_motion(pixel, velocity, context=None):
    fail=lambda why:dict(geographic_verified=False,geographic_reason=why,bearing_deg=None,bearing_label=None)
    if context is None:return fail('동기 자세·깊이·수직 이동 미확인')
    if not all(context.get(k) is True for k in ('calibration_verified','synchronization_verified','enu_alignment_verified','vertical_velocity_verified')):
        return fail('좌표 검증 불충분')
    try:
        if not 0<=context['pose_age_sec']<=.25:return fail('자세 시각 불일치')
        R=np.asarray(context['world_from_camera'],float);depth=float(context['forward_depth_m'])
        fx,fy,cx,cy=map(float,context['intrinsics']);u,v=map(float,pixel)
        flow=np.asarray(velocity,float);vz=float(context['vertical_velocity_m_s'])
        if R.shape!=(3,3) or not np.isfinite(R).all() or not np.allclose(R.T@R,np.eye(3),atol=1e-5) or abs(np.linalg.det(R)-1)>1e-5:return fail('회전행렬 오류')
        if not np.isfinite([depth,fx,fy,cx,cy,u,v,vz,*flow]).all() or depth<=0 or min(fx,fy)<=0:return fail('유효하지 않은 기하')
        # Gazebo 카메라 +X 전방,+Y 왼쪽,+Z 위. 영상 u는 오른쪽,v는 아래.
        y=-(u-cx)*depth/fx;z=-(v-cy)*depth/fy
        J=np.array([[fx*y/depth**2,-fx/depth,0],[fy*z/depth**2,0,-fy/depth]])@R.T
        A=J[:,:2]
        if np.linalg.cond(A)>25:return fail('수평·수직 이동 분리 불안정')
        east,north=np.linalg.solve(A,flow-J[:,2]*vz)
        speed=math.hypot(east,north)
        if speed<.1:return fail('수평 이동 증거 부족')
        noise=float(context['pixel_velocity_error_bound'])
        if noise<0 or not math.isfinite(noise):return fail('방위 불확실성 과다')
        error_bound=float(np.linalg.norm(np.linalg.inv(A),ord=2)*noise)
        if error_bound>speed*.25:return fail('방위 불확실성 과다')
        bearing=math.degrees(math.atan2(east,north))%360
        angle_error=math.degrees(math.asin(min(1,error_bound/speed)))
        if 22.5-abs((bearing+22.5)%45-22.5)<=angle_error:return fail('방위 구간 경계 불확실')
        return dict(geographic_verified=True,geographic_reason='검증된 ENU 영상 이동',bearing_deg=bearing,
            bearing_label=('N','NE','E','SE','S','SW','W','NW')[int((bearing+22.5)//45)%8],
            east_m_s=float(east),north_m_s=float(north),bearing_error_bound_deg=angle_error,wind_verified=False)
    except (KeyError,TypeError,ValueError,np.linalg.LinAlgError):return fail('필수 기하 자료 누락')
