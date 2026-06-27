#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_bin="${DOCKER:-docker}"
tag="${CCC_DOCKER_TAG:-ccc-layered-storage:smoke-local}"

if ! command -v "$docker_bin" >/dev/null 2>&1; then
  echo "docker not found; install Docker or set DOCKER=/path/to/docker" >&2
  exit 2
fi

if [ ! -c /dev/fuse ]; then
  echo "/dev/fuse is not available on the host; Docker FUSE smoke cannot run" >&2
  exit 3
fi

"$docker_bin" build -t "$tag" "$repo_root"

"$docker_bin" run --rm \
  --device /dev/fuse \
  --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  --tmpfs /tmp:rw,nosuid,nodev,exec,mode=1777 \
  "$tag" \
  sh -lc '
    set -eu
    make test
    CCC_SMOKE_ROOT=/tmp/ccc-layered-docker-smoke/runtime deploy/runtime-smoke.sh
    CCC_SMOKE_ROOT=/tmp/ccc-layered-docker-smoke/fuse deploy/fuse-smoke.sh
  '

echo "docker smoke passed"
