#!/bin/zsh
# Same world/model names and proven OGRE2 Metal launch path as the working X500.
set -eu
script_dir="${0:A:h}"
industrial_dir="$script_dir/industrial"
stage="${1:-worker}"
case "$stage" in
  worker) world="$industrial_dir/worker_only.sdf"; asset_args=(--check) ;;
  site) world="$industrial_dir/industrial_site.sdf"; asset_args=(--check --site) ;;
  effects) world="$industrial_dir/industrial_effects.sdf"; asset_args=(--check --site) ;;
  *) print -u2 "Usage: zsh run_industrial_world.sh [worker|site|effects]"; exit 2 ;;
esac
px4_dir="${PX4_SOURCE_DIR:-$HOME/Desktop/PX4-Autopilot}"
asset_python="${GAZEBO_PYTHON:-$px4_dir/Tools/setup/macos/.pixi/envs/default/bin/python}"
"$asset_python" "$industrial_dir/prepare_assets.py" "${asset_args[@]}"
export GZ_PARTITION="${GZ_PARTITION:-safety_x500_camera}"
export GZ_SIM_RESOURCE_PATH="$industrial_dir/models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
exec zsh "$script_dir/run_camera_probe.sh" "$world"
