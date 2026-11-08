#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
if [[ -n "${DRONE_PYTHON:-}" ]]; then
    python_bin="$DRONE_PYTHON"
elif [[ -x .venv/bin/python ]]; then
    python_bin="$PWD/.venv/bin/python"
elif [[ -x /opt/homebrew/Caskroom/miniforge/base/envs/vision-ai/bin/python ]]; then
    python_bin=/opt/homebrew/Caskroom/miniforge/base/envs/vision-ai/bin/python
else
    python_bin=python3
fi
for model in yolo11s.pt yolo11n-pose.pt ppe_best.pt; do
    if [[ ! -f "app/models/$model" ]]; then
        printf 'app/models/%s 파일이 필요합니다. README.md의 실행 준비를 확인하세요.\n' "$model" >&2
        exit 1
    fi
done
cd app
if [[ $# -eq 0 ]]; then
    set -- --camera-index 0
fi
exec "$python_bin" -B -m tools.integration --mode OFFLINE_DEMO --no-legacy-import "$@"
