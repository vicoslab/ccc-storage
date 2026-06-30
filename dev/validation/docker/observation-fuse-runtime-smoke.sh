#!/usr/bin/env bash
set -euo pipefail

# Live pyfuse3 marker-observation runtime smoke.
#
# Validates the deployable transparent path:
#   - ccc-storage mountd serves a live pyfuse3 observation root;
#   - mkdir through the published folder creates a generation-0 observed child;
#   - the live FUSE mkdir path mounts a writable shared-overlay child immediately;
#   - writes land in the shared overlay, not the source tree;
#   - commit creates a SquashFS delta and clears the overlay;
#   - after unmount/remount, committed data is readable through the same path.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
docker_bin="${DOCKER:-docker}"
python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  if command -v python >/dev/null 2>&1; then
    python_bin="python"
  elif command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  else
    python_bin="python"
  fi
fi

tag="${CCC_DOCKER_TAG:-ccc-layered-storage:observation-fuse-runtime-local}"
runtime_root_input="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-observation-fuse-test}"
runtime_docker_source_root_input="${CCC_RUNTIME_DOCKER_SOURCE_ROOT:-}"

resolve_path() {
  "$python_bin" - "$1" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
}

safe_name() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '-' | sed 's/^-*//; s/-*$//'
}

validate_runtime_root() {
  local root_real="$1"
  local repo_real="$2"
  case "$root_real" in
    /|/storage|/storage/user|/storage/datasets|/storage/group|/home|/tmp|"$repo_real"|"$repo_real/.scratch")
      echo "refusing unsafe CCC_RUNTIME_ROOT: $root_real" >&2
      exit 2
      ;;
  esac
  case "$root_real" in
    /storage/user/*|/tmp/*|"$repo_real"/.scratch/*) ;;
    *)
      echo "refusing unsafe CCC_RUNTIME_ROOT: $root_real" >&2
      exit 2
      ;;
  esac
}

validate_runtime_docker_source_root() {
  local root_real="$1"
  case "$root_real" in
    /|/storage|/storage/user|/storage/datasets|/storage/group|/home|/opt|/opt/shared_storage)
      echo "refusing unsafe CCC_RUNTIME_DOCKER_SOURCE_ROOT: $root_real" >&2
      exit 2
      ;;
  esac
}

if ! command -v "$docker_bin" >/dev/null 2>&1; then
  echo "docker not found; install Docker or set DOCKER=/path/to/docker" >&2
  exit 2
fi
if [ ! -c /dev/fuse ]; then
  echo "/dev/fuse is not available on the host; live observation FUSE smoke cannot run" >&2
  exit 3
fi

repo_real="$(resolve_path "$repo_root")"
runtime_root_real="$(resolve_path "$runtime_root_input")"
validate_runtime_root "$runtime_root_real" "$repo_real"
if [ -n "$runtime_docker_source_root_input" ]; then
  docker_source_root_real="$(resolve_path "$runtime_docker_source_root_input")"
else
  docker_source_root_real="$runtime_root_real"
fi
validate_runtime_docker_source_root "$docker_source_root_real"

hostname_safe="$(safe_name "$(hostname -s 2>/dev/null || hostname || printf host)")"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_id_safe="$(safe_name "${hostname_safe}-${timestamp}-$$")"
run_root="$runtime_root_real/runs/$run_id_safe"
docker_run_root_source="$docker_source_root_real/runs/$run_id_safe"

cleanup() {
  if [ "${CCC_RUNTIME_KEEP:-}" = "1" ]; then
    echo "kept runtime root: $run_root"
  else
    rm -rf "$run_root"
  fi
}
trap cleanup EXIT

if [ "${CCC_SKIP_BUILD:-}" != "1" ]; then
  "$docker_bin" build -t "$tag" "$repo_root"
else
  echo "skipping docker build because CCC_SKIP_BUILD=1"
fi

mkdir -p "$run_root"
if [ "$docker_source_root_real" != "$runtime_root_real" ]; then
  if ! mkdir -p "$docker_run_root_source" 2>/dev/null; then
    "$docker_bin" run --rm --privileged \
      --mount "type=bind,src=/,dst=/host" \
      "$tag" \
      mkdir -p "/host$docker_run_root_source"
  fi
fi

echo "per-run root: $run_root"
echo "Docker bind source root: $docker_run_root_source"

run_cmd=("$docker_bin" run --rm -i
  --privileged
  --device /dev/fuse:/dev/fuse:rwm
  --security-opt apparmor=unconfined
  --security-opt seccomp=unconfined
  --mount "type=bind,src=$docker_run_root_source,dst=/ccc-runtime,bind-propagation=rshared"
  "$tag"
  sh -s)

if command -v timeout >/dev/null 2>&1; then
  run_cmd=(timeout "${CCC_OBSERVATION_FUSE_TIMEOUT:-180}" "${run_cmd[@]}")
fi

"${run_cmd[@]}" <<'SH'
set -euo pipefail

root=/ccc-runtime
nfs=$root/nfs
run=$root/run
source=$root/source
published=$root/published
socket_path=$run/mountd.sock
mountd_log=$root/mountd.log
mountd_pid=""

cleanup() {
  set +e
  export CCC_NFS_ROOT="$nfs" CCC_MOUNTD_SOCK="$socket_path"
  ccc-storage umount observe:new-env --json >/dev/null 2>&1 || true
  if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u -z "$published/new-env" >/dev/null 2>&1 || true
    fusermount3 -u -z "$published" >/dev/null 2>&1 || true
  fi
  if mountpoint -q "$published/new-env"; then
    umount -l "$published/new-env" >/dev/null 2>&1 || true
  fi
  if mountpoint -q "$published"; then
    umount -l "$published" >/dev/null 2>&1 || true
  fi
  if [ -n "$mountd_pid" ] && kill -0 "$mountd_pid" 2>/dev/null; then
    kill "$mountd_pid" >/dev/null 2>&1 || true
    wait "$mountd_pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$nfs/registry" "$nfs/packs" "$nfs/overlays" "$nfs/locks" "$run" "$source" "$published"
printf '' >"$source/CCC_LAYERED_OBSERVE"

python - <<'PY'
import pyfuse3  # noqa: F401
import trio  # noqa: F401
print('pyfuse3/trio import ok')
PY

export CCC_NFS_ROOT="$nfs"
export CCC_NODE_RUN_DIR="$run"
export CCC_MOUNTD_SOCK="$socket_path"

ccc-storage mountd \
  --nfs-root "$nfs" \
  --run-dir "$run" \
  --socket "$socket_path" \
  --observe-root "$source" \
  --observe-mountpoint "$published" >"$mountd_log" 2>&1 &
mountd_pid="$!"

for _ in $(seq 1 120); do
  if ! kill -0 "$mountd_pid" 2>/dev/null; then
    echo "ccc-storage mountd exited before ready" >&2
    sed -n '1,240p' "$mountd_log" >&2 || true
    exit 1
  fi
  if [ -S "$socket_path" ] && mountpoint -q "$published"; then
    break
  fi
  sleep 0.1
done
if [ ! -S "$socket_path" ] || ! mountpoint -q "$published"; then
  echo "observation dispatcher did not become ready" >&2
  sed -n '1,240p' "$mountd_log" >&2 || true
  findmnt -R "$root" >&2 || true
  exit 1
fi

ccc-storage doctor --json >"$root/doctor.json"
ccc-storage observe-ls --json >"$root/observe-before.json"

mkdir "$published/new-env"
for _ in $(seq 1 80); do
  if mountpoint -q "$published/new-env"; then
    break
  fi
  sleep 0.05
done
if ! mountpoint -q "$published/new-env"; then
  echo "new observed child was not mounted after live FUSE mkdir" >&2
  ccc-storage status observe:new-env --json >&2 || true
  sed -n '1,240p' "$mountd_log" >&2 || true
  findmnt -R "$root" >&2 || true
  exit 1
fi

printf 'created through live pyfuse3 dispatcher\n' >"$published/new-env/created.txt"
mkdir -p "$published/new-env/nested"
printf 'nested payload\n' >"$published/new-env/nested/payload.txt"
sync

if [ -e "$source/new-env/created.txt" ]; then
  echo "write leaked into source tree instead of overlay" >&2
  exit 1
fi
if [ ! -f "$nfs/overlays/observe%3Anew-env/active/created.txt" ]; then
  echo "write did not land in shared overlay upper" >&2
  find "$nfs/overlays" -maxdepth 5 -type f -print >&2 || true
  exit 1
fi

ccc-storage status observe:new-env --json >"$root/status-before-commit.json"
ccc-storage umount observe:new-env --json >"$root/pre-commit-umount.json"
for _ in $(seq 1 80); do
  if ! mountpoint -q "$published/new-env"; then
    break
  fi
  sleep 0.05
done
if mountpoint -q "$published/new-env"; then
  echo "new-env remained mounted after pre-commit umount" >&2
  findmnt -R "$root" >&2 || true
  exit 1
fi

ccc-storage commit observe:new-env -m 'live observation FUSE smoke' --json >"$root/commit.json"
python - <<'PY'
import json
from pathlib import Path
root = Path('/ccc-runtime')
data = json.loads((root / 'commit.json').read_text())
if data['generation'] != 1:
    raise SystemExit(f"expected generation 1 after generation-0 commit, got {data['generation']}")
if len(data.get('packs', [])) != 1:
    raise SystemExit(f"expected exactly one committed delta pack, got {data.get('packs')!r}")
if data['overlay']['dirty']:
    raise SystemExit('overlay remained dirty after commit')
pack = Path(data['packs'][0]['path'])
if not pack.is_file():
    raise SystemExit(f'committed pack missing: {pack}')
print('commit json ok')
PY

# Re-access the same path through the live dispatcher; lookup should lazily mount
# the committed SquashFS stack and expose the just-committed file.
for _ in $(seq 1 80); do
  if [ -r "$published/new-env/created.txt" ]; then
    break
  fi
  sleep 0.05
done
if [ "$(cat "$published/new-env/created.txt")" != "created through live pyfuse3 dispatcher" ]; then
  echo "committed file was not readable after remount" >&2
  ccc-storage status observe:new-env --json >&2 || true
  sed -n '1,240p' "$mountd_log" >&2 || true
  findmnt -R "$root" >&2 || true
  exit 1
fi
if [ "$(cat "$published/new-env/nested/payload.txt")" != "nested payload" ]; then
  echo "committed nested file was not readable after remount" >&2
  exit 1
fi
ccc-storage status observe:new-env --json >"$root/status-after-remount.json"

python - <<'PY'
import json
from pathlib import Path
root = Path('/ccc-runtime')
status = json.loads((root / 'status-after-remount.json').read_text())
summary = {
    'id': status['id'],
    'generation': status['generation'],
    'mounted': status['mounted'],
    'mountpoint': status['mountpoint'],
    'pack_count': len(status.get('packs', [])),
    'overlay_dirty': status['overlay']['dirty'],
}
print(json.dumps(summary, indent=2, sort_keys=True))
PY

echo 'live observation FUSE runtime smoke passed: generation-0 write, commit, remount'
SH
