#!/bin/zsh
# Attach the existing PX4 build to the model already included in x500_camera.sdf.
set -eu
script_dir="${0:A:h}"
project_dir="${script_dir:h:h}"
px4_dir="${PX4_SOURCE_DIR:-$HOME/Desktop/PX4-Autopilot}"
build_dir="$px4_dir/build/px4_sitl_default"
pixi_prefix="$px4_dir/Tools/setup/macos/.pixi/envs/default"
if [[ ! -x "$build_dir/bin/px4" || ! -f "$build_dir/etc/init.d-posix/rcS" ]]; then
  print -u2 "Existing PX4 SITL build missing: $build_dir"
  exit 1
fi
export PATH="$pixi_prefix/bin:$PATH"
export GZ_IP=127.0.0.1
export GZ_PARTITION="${GZ_PARTITION:-safety_x500_camera}"
export OGRE_RESOURCE_PATH="$pixi_prefix/lib/OGRE"
export OGRE2_RESOURCE_PATH="$pixi_prefix/lib/OGRE-Next"
export PX4_SYS_AUTOSTART=4010
export PX4_SIM_MODEL=gz_x500_mono_cam
export PX4_GZ_STANDALONE=1
export PX4_GZ_WORLD=x500_camera
export PX4_GZ_MODEL_NAME=x500_mono_cam_0
export PX4_GZ_MODELS="$px4_dir/Tools/simulation/gz/models"
export PX4_GZ_WORLDS="$px4_dir/Tools/simulation/gz/worlds"
export GZ_SIM_RESOURCE_PATH="$PX4_GZ_MODELS:$PX4_GZ_WORLDS${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$build_dir/src/modules/simulation/gz_plugins${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
print "[X500 PX4] world=$PX4_GZ_WORLD model=$PX4_GZ_MODEL_NAME partition=$GZ_PARTITION instance=0"
"$pixi_prefix/bin/python" "$script_dir/check_x500_sync.py" --role px4 \
  --world "$PX4_GZ_WORLD" --model "$PX4_GZ_MODEL_NAME" \
  --output "$project_dir/diagnostics/gazebo/sensor_sync/px4-preflight.json"
# Keep parameters, logs and dataman inside this project.
runtime_dir="$project_dir/diagnostics/gazebo/x500/sitl"
mkdir -p "$runtime_dir"
cd "$runtime_dir"
exec "$build_dir/bin/px4" -i 0 -w "$runtime_dir" -s "$build_dir/etc/init.d-posix/rcS" "$build_dir/etc"
