#!/bin/zsh
# Run from a regular macOS terminal; does not modify PX4 or ~/.gz settings.
set -eu
probe_dir="${0:A:h}"
px4_dir="${PX4_SOURCE_DIR:-$HOME/Desktop/PX4-Autopilot}"
pixi_prefix="$px4_dir/Tools/setup/macos/.pixi/envs/default"
export PATH="$pixi_prefix/bin:$PATH"
export GZ_IP=127.0.0.1
export GZ_PARTITION="${GZ_PARTITION:-safety_camera_probe}"
# gz-rendering reads these variables directly. OGRE_PLUGIN_DIR is different.
export OGRE_RESOURCE_PATH="$pixi_prefix/lib/OGRE"
export OGRE2_RESOURCE_PATH="$pixi_prefix/lib/OGRE-Next"
export GZ_SIM_RESOURCE_PATH="$px4_dir/Tools/simulation/gz/models:$px4_dir/Tools/simulation/gz/worlds${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
px4_plugins="$px4_dir/build/px4_sitl_default/src/modules/simulation/gz_plugins"
if [[ -d "$px4_plugins" ]]; then
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$px4_plugins${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
fi
if [[ ! -x "$pixi_prefix/bin/gz" ]]; then
  print -u2 "Gazebo not found: $pixi_prefix/bin/gz"
  exit 1
fi
exec "$pixi_prefix/bin/gz" sim -s -r -v 4 \
  --render-engine-server ogre2 --render-engine-server-api-backend metal \
  "${1:-$probe_dir/camera_probe.sdf}"
