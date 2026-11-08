"""Mac video-only input through the already installed FFmpeg AVFoundation backend.
No permission changes, microphone input, screen capture or automatic installs.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys


def executable():
    value=shutil.which('ffmpeg',path=str(Path(sys.executable).parent)) or shutil.which('ffmpeg')
    if not value:raise FileNotFoundError('FFmpeg missing; installation requires separate approval')
    return value


def parse_devices(stderr):
    rows=[];video=False
    for line in stderr.splitlines():
        if 'AVFoundation video devices:' in line:video=True;continue
        if 'AVFoundation audio devices:' in line:video=False
        match=re.search(r'\[(\d+)\] (.+)$',line)
        if video and match and not match[2].lower().startswith('capture screen'):
            rows.append((int(match[1]),match[2]))
    return rows


def devices():
    if sys.platform!='darwin':raise ValueError('WEBCAM_BACKEND_MAC_ONLY_UNVALIDATED_OTHER_OS')
    # FFmpeg exits 1 after a successful list-only query; no device is opened.
    result=subprocess.run([executable(),'-hide_banner','-f','avfoundation','-list_devices','true','-i',''],capture_output=True,text=True,timeout=8)
    if 'AVFoundation video devices:' not in result.stderr:raise RuntimeError('CAMERA_ENUMERATION_FAILED: '+result.stderr[-1000:])
    return parse_devices(result.stderr)


def capture_command(index):
    if type(index) is not int or index<0:raise ValueError('EXPLICIT_CAMERA_INDEX_REQUIRED')
    if index not in dict(devices()):raise ValueError('CAMERA_NOT_LISTED_OR_SCREEN_INPUT_REJECTED')
    return [executable(),'-hide_banner','-loglevel','warning','-nostdin','-f','avfoundation','-framerate','30','-i',str(index)+':none',
            '-an','-sn','-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2',
            '-fps_mode','passthrough','-pix_fmt','bgr24','-f','rawvideo','pipe:1']
