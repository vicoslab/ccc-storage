#!/usr/bin/env bash
set -euo pipefail

# Dedicated mountd-container topology smoke.
#
# Validates the production shape:
#   mountd container: owns /dev/fuse + SYS_ADMIN and serves the live observation root
#   app container: unprivileged, no /dev/fuse, no mountd socket/env, sees only an rslave storage bind

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image_tag="${CCC_MOUNTD_IMAGE:-ccc-storage-mountd:local}"
app_image="${CCC_APP_IMAGE:-$image_tag}"
runtime_root="${CCC_RUNTIME_ROOT:-/storage/user/ccc-storage-mountd-container-test}"
run_id="${CCC_RUNTIME_RUN_ID:-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_root="$runtime_root/runs/$run_id"
docker_source_root="${CCC_RUNTIME_DOCKER_SOURCE_ROOT:-}"
keep="${CCC_RUNTIME_KEEP:-0}"
skip_build="${CCC_RUNTIME_SKIP_BUILD:-0}"
timeout_s="${CCC_MOUNTD_CONTAINER_TIMEOUT:-180}"
mountd_name="ccc-storage-mountd-test-$run_id"
app_name="ccc-storage-app-test-$run_id"

docker_bin="${DOCKER:-docker}"

cleanup() {
  "$docker_bin" rm -f "$app_name" "$mountd_name" >/dev/null 2>&1 || true
  if [ "$keep" != "1" ]; then
    rm -rf "$run_root" 2>/dev/null || true
  else
    echo "kept runtime root: $run_root"
  fi
}
trap cleanup EXIT

mkdir -p "$run_root"/{nfs,source,published}
touch "$run_root/source/CCC_STORAGE_OBSERVE"

if [ "$skip_build" != "1" ]; then
  "$docker_bin" build -f "$repo_root/deploy/docker/mountd.Dockerfile" -t "$image_tag" "$repo_root"
fi

if [ -z "$docker_source_root" ]; then
  docker_run_root="$run_root"
else
  docker_run_root="$docker_source_root/runs/$run_id"
  if ! mkdir -p "$docker_run_root" 2>/dev/null; then
    "$docker_bin" run --rm --privileged --mount type=bind,src=/,dst=/host "$image_tag" \
      sh -lc "mkdir -p /host$(printf '%q' "$docker_run_root")"
  fi
fi

# The mountd container gets a shared bind so its FUSE mounts can propagate to the host.
"$docker_bin" run -d --rm \
  --name "$mountd_name" \
  --device /dev/fuse:/dev/fuse:rw \
  --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  --mount type=bind,src="$docker_run_root",dst=/ccc-runtime,bind-propagation=rshared \
  -e CCC_NFS_ROOT=/ccc-runtime/nfs \
  -e CCC_NODE_RUN_DIR=/run/ccc-storage \
  -e CCC_MOUNTD_SOCK=/run/ccc-storage/mountd.sock \
  -e CCC_OBSERVE_ROOT=/ccc-runtime/source \
  -e CCC_OBSERVE_MOUNTPOINT=/ccc-runtime/published \
  -e CCC_MOUNTD_SOCKET_MODE=0600 \
  "$image_tag" >/dev/null

wait_for_mount() {
  local container=$1
  local target=$2
  local deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$docker_bin" exec "$container" sh -lc "findmnt -T '$target' >/dev/null 2>&1"; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for $target to be mounted in $container" >&2
  "$docker_bin" logs "$mountd_name" >&2 || true
  return 1
}

wait_for_mount "$mountd_name" /ccc-runtime/published

# The app container gets only the propagated published storage.  It does not get
# /dev/fuse, SYS_ADMIN, mountd socket, or mountd env vars.
"$docker_bin" run -d --rm \
  --name "$app_name" \
  --mount type=bind,src="$docker_run_root/published",dst=/storage/layered,bind-propagation=rslave \
  "$app_image" sh -lc 'while true; do sleep 3600; done' >/dev/null

"$docker_bin" exec "$app_name" sh -lc '
  set -euo pipefail
  if env | grep -E "^CCC_(MOUNTD|NFS_ROOT|NODE_RUN_DIR|OBSERVE|LAYERED_STORAGE)"; then
    echo "app container unexpectedly has mountd/layered CCC env" >&2
    exit 1
  fi
  test ! -S /run/ccc-storage/mountd.sock
  test ! -e /dev/fuse
  prop="$(findmnt -T /storage/layered -no PROPAGATION 2>/dev/null || true)"
  case "$prop" in *shared*|*slave*) ;; *) echo "unexpected app propagation: ${prop:-unknown}" >&2; exit 1 ;; esac
'

"$docker_bin" exec "$app_name" sh -lc 'mkdir /storage/layered/new-env'

# mkdir triggers a background child mount; wait for it from both sides.
wait_for_mount "$mountd_name" /ccc-runtime/published/new-env
wait_for_mount "$app_name" /storage/layered/new-env

"$docker_bin" exec "$app_name" sh -lc '
  set -euo pipefail
  mkdir -p /storage/layered/new-env/nested
  printf app-write > /storage/layered/new-env/created.txt
  printf payload > /storage/layered/new-env/nested/payload.txt
  sync /storage/layered/new-env/created.txt 2>/dev/null || sync
'

if [ -e "$run_root/source/new-env/created.txt" ]; then
  echo "app write leaked into source tree instead of overlay" >&2
  exit 1
fi
if [ ! -e "$run_root/nfs/overlays/observe%3Anew-env/active/created.txt" ]; then
  echo "app write did not reach shared overlay upper" >&2
  exit 1
fi

"$docker_bin" exec "$mountd_name" ccc-storage umount observe:new-env --json >/tmp/ccc-storage-mountd-container-umount.json
"$docker_bin" exec "$mountd_name" ccc-storage commit observe:new-env --json >/tmp/ccc-storage-mountd-container-commit.json

"${PYTHON:-python3}" - <<'PY'
import json
from pathlib import Path
path = Path('/tmp/ccc-storage-mountd-container-commit.json')
data = json.loads(path.read_text())
assert data['generation'] == 1, data
assert data['overlay']['dirty'] is False, data
assert len(data['packs']) == 1, data
print('commit json ok')
PY

# Trigger a remount through the app-visible dispatcher path and read committed data.
wait_for_committed_read() {
  local deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$docker_bin" exec "$app_name" sh -lc \
      'test "$(cat /storage/layered/new-env/created.txt 2>/dev/null)" = app-write && test "$(cat /storage/layered/new-env/nested/payload.txt 2>/dev/null)" = payload'; then
      return 0
    fi
    "$docker_bin" exec "$app_name" sh -lc 'ls -la /storage/layered >/dev/null 2>&1; ls -la /storage/layered/new-env >/dev/null 2>&1 || true' || true
    sleep 1
  done
  echo "timed out waiting for committed data through app-visible layered mount" >&2
  "$docker_bin" logs "$mountd_name" >&2 || true
  return 1
}
wait_for_committed_read

printf 'mountd container topology smoke passed: dedicated service + unprivileged app + commit/remount\n'
