# Third-party notices for v0.1

`tools/gazebo/models/mono_cam_clear` and `x500_mono_cam_clear` are project-local variants derived from `mono_cam` and `x500_mono_cam` in [PX4/PX4-gazebo-models](https://github.com/PX4/PX4-gazebo-models), inspected local revision `a15af9628536914ff7201c992fce5e3cb5d70db9`. The monocular sensor optical pose was shifted forward 0.22m and the wrapper references the local camera variant. These existing modifications are preserved in this upload preparation.

The source repository root BSD 3-Clause notice is reproduced without modification in [PX4-gazebo-models-BSD-3-Clause.txt](third_party/PX4-gazebo-models-BSD-3-Clause.txt). Upstream `mono_cam` metadata credits Markus Achtelik. Upstream X500 geometry remains an external model reference; X500 meshes are not copied into this source bundle, and their separate notices remain with the external PX4 installation.

Industrial generated geometry retains `tools/gazebo/industrial/LICENSE.generated-assets.txt`. External CC0 worker and texture binaries are excluded; sources are listed in `assets.json`. YOLO/PPE/fire weights and test datasets are excluded from the new bundle; see [LICENSE_REVIEW.md](LICENSE_REVIEW.md) for unresolved distribution rights and already tracked remote assets. These notices do not assign a new license to the entire project.
