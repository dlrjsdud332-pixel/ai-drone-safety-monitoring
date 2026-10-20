# macOS camera diagnosis (2026-10-19)

## Observed evidence

- macOS 27.0.1, arm64; PX4 `v1.18.0-beta1-1127-g44159002d3-dirty`.
- Pixi: Gazebo Sim 8.10.0, gz-rendering 8.2.2, OGRE 1.10.12.1,
  ogre-next 2.3.3. Both Metal and GL3Plus plugins exist.
- Original `~/.gz/rendering/ogre.log` contains embedded NULs between
  `.../lib/OGRE` and `/RenderSystem_GL.dylib`. The actual loader error says
  `dlopen(.../lib/OGRE)` / `not a file`.
- `ogre2.log` has the same defect for `.../lib/OGRE-Next`, including Metal.
- Installed gz-rendering engine binaries contain the corresponding relocated
  prefix with NUL padding. This is consistent with a binary prefix relocation
  problem combined with a compiled string length; the precise compiled
  construction has not been confirmed by disassembly or a rebuild.
- Existing crash report `ruby-2026-10-19-174000.ips` identifies
  `OgreRenderEngine::CreateRenderSystem()` and a null address.
- `libMotorFailurePlugin.dylib` exists in the PX4 build. Its error needs plugin
  search-path validation separately; it does not establish a camera/GPU fault.

Raw baseline files and the attempted run log are in `diagnostics/gazebo/`.
The existing tracker, PX4 model, world and user server config were not edited.

## Minimal candidate workaround — not yet verified

gz-rendering reads `OGRE_RESOURCE_PATH` / `OGRE2_RESOURCE_PATH`, independently
of `OGRE_PLUGIN_DIR` and `plugins.cfg`. The launcher supplies these and selects
OGRE2 Metal. This tests a different path from the previously failed attempts.
It also supplies the existing PX4 system-plugin build directory.

From the project root in an ordinary macOS terminal:

```sh
zsh tools/gazebo/run_camera_probe.sh 2>&1 | tee diagnostics/gazebo/probe-terminal.log
```

In a second terminal, from the same project root:

```sh
export GZ_IP=127.0.0.1
export GZ_PARTITION=safety_camera_probe
~/Desktop/PX4-Autopilot/Tools/setup/macos/.pixi/envs/default/bin/python tools/gazebo/receive_probe.py
```

If you use a custom GZ_PARTITION, use exactly the same value in both terminals.
Success requires ten valid RGB messages and a saved `frame.ppm` showing the
red box; a running server or listed topic alone is insufficient. Stop the
probe with Ctrl-C before progressing to the PX4 world.

## Validation limit and next gate

This Codex session's sandbox rejects local socket binding:
`Binding to a local port failed` and `InitializeSockets() Error: Operation not permitted`.
The probe therefore did not reach a valid renderer or image delivery test.
The attempted run timed out; no image-generation success is claimed.
Python syntax, SDF XML syntax, shell syntax, installed message enums and
transport imports were checked. These do not validate rendering.

After the probe passes, test the unchanged safety world with the launcher
(first positional argument is a world file), a matching PX4 standalone
partition, and the actual RGB topic. Then add a Gazebo input adapter to the
existing tracker. Tracker integration, PX4 flight, telemetry regression,
and YOLO person/fire/smoke validation remain pending. The simple box and
placeholder people/flame visuals are not an object-detection benchmark.

Upstream references:
- https://github.com/gazebosim/gz-rendering/blob/gz-rendering8/ogre/src/OgreRenderEngine.cc
- https://github.com/gazebosim/gz-rendering/blob/gz-rendering8/ogre2/src/Ogre2RenderEngine.cc
- https://github.com/gazebosim/gz-sim/issues/960

If correct plugin loading reveals a Cocoa/main-thread or backend error,
capture that new error before changing anything else. Existing evidence
does not justify a VM or reinstall, and does not yet prove full macOS support
for this installed combination.
