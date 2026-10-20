#!/bin/zsh
# Uses the proven probe's OGRE2 Metal launcher without changing its defaults.
set -eu
script_dir="${0:A:h}"
export GZ_PARTITION="${GZ_PARTITION:-safety_x500_camera}"
export GZ_SIM_RESOURCE_PATH="$script_dir/models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export GZ_IP=127.0.0.1
px4_dir="${PX4_SOURCE_DIR:-$HOME/Desktop/PX4-Autopilot}"
pixi_python="$px4_dir/Tools/setup/macos/.pixi/envs/default/bin/python"
print "[X500 Gazebo] world=x500_camera model=x500_mono_cam_0 partition=$GZ_PARTITION"
"$pixi_python" "$script_dir/check_x500_sync.py" --role gazebo \
  --output "$script_dir/../../diagnostics/gazebo/sensor_sync/gazebo-preflight.json"
exec zsh "$script_dir/run_camera_probe.sh" "$script_dir/x500_camera.sdf"
