#!/usr/bin/env bash
set -euo pipefail

# Runtime smoke for the primary observation-directory mode:
#   OBSERVATION_DIR is mounted in place as a passthrough FUSE root;
#   OBSERVATION_DIR/.ccc-storage lives on the private NFS backing source;
#   top-level mkdir creates a managed child;
#   managed-child operations route through a private child mount under run/;
#   ordinary unmanaged file I/O passes through to the backing source.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
docker_bin="${DOCKER:-docker}"
image_tag="${CCC_MOUNTD_IMAGE:-ccc-storage-mountd:obsdir-dev}"
runtime_root="${CCC_RUNTIME_ROOT:-/storage/user/ccc-storage-observation-dir-test}"
docker_source_root="${CCC_RUNTIME_DOCKER_SOURCE_ROOT:-$runtime_root}"
run_id="${CCC_RUNTIME_RUN_ID:-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_root="$runtime_root/runs/$run_id"
docker_run_root="$docker_source_root/runs/$run_id"
keep="${CCC_RUNTIME_KEEP:-0}"
skip_build="${CCC_RUNTIME_SKIP_BUILD:-0}"
timeout_s="${CCC_OBSERVATION_DIR_TIMEOUT:-240}"

cleanup() {
  set +e
  if [ "$keep" != "1" ]; then
    rm -rf "$run_root" "$docker_run_root" 2>/dev/null || true
  else
    echo "kept runtime root: $run_root"
  fi
}
trap cleanup EXIT

if ! command -v "$docker_bin" >/dev/null 2>&1; then
  echo "docker not found; set DOCKER=/path/to/docker" >&2
  exit 2
fi
if [ ! -c /dev/fuse ]; then
  echo "/dev/fuse is not available on host" >&2
  exit 3
fi

mkdir -p "$run_root"
if [ "$docker_run_root" != "$run_root" ]; then
  if ! mkdir -p "$docker_run_root" 2>/dev/null; then
    "$docker_bin" run --rm --privileged \
      --mount "type=bind,src=/,dst=/host" \
      "$image_tag" \
      mkdir -p "/host$docker_run_root"
  fi
fi
if [ "$skip_build" != "1" ]; then
  "$docker_bin" build -f "$repo_root/deploy/docker/mountd.Dockerfile" -t "$image_tag" "$repo_root"
fi

run_cmd=("$docker_bin" run --rm -i
  --privileged
  --device /dev/fuse:/dev/fuse:rwm
  --security-opt apparmor=unconfined
  --security-opt seccomp=unconfined
  --mount "type=bind,src=$docker_run_root,dst=/ccc-runtime,bind-propagation=rshared"
  "$image_tag"
  sh -s)

if command -v timeout >/dev/null 2>&1; then
  run_cmd=(timeout "$timeout_s" "${run_cmd[@]}")
fi

"${run_cmd[@]}" <<'SH'
set -euo pipefail

root=/ccc-runtime
obs=$root/obs
run=$root/run
socket_path=$run/mountd.sock
config=$root/mountd.toml
mountd_log=$root/mountd.log
mountd_pid=""

is_mountpoint() {
  findmnt -n -o TARGET --target "$1" 2>/dev/null | grep -Fx -- "$1" >/dev/null
}

cleanup() {
  set +e
  export CCC_MOUNTD_SOCK="$socket_path"
  ccc-storage umount env-a --json >/dev/null 2>&1 || true
  ccc-storage umount env-x --json >/dev/null 2>&1 || true
  if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u -z "$obs/unmanaged/envs/env-x" >/dev/null 2>&1 || true
    fusermount3 -u -z "$obs/unmanaged/envs" >/dev/null 2>&1 || true
    fusermount3 -u -z "$obs/env-a" >/dev/null 2>&1 || true
    fusermount3 -u -z "$obs" >/dev/null 2>&1 || true
  fi
  for target in "$obs/unmanaged/envs/env-x" "$obs/unmanaged/envs" "$obs/env-a" "$obs"; do
    if is_mountpoint "$target"; then
      umount -l "$target" >/dev/null 2>&1 || true
    fi
  done
  if [ -n "$mountd_pid" ] && kill -0 "$mountd_pid" 2>/dev/null; then
    kill "$mountd_pid" >/dev/null 2>&1 || true
    wait "$mountd_pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$obs/unmanaged/envs" "$run"
printf 'old-root\n' >"$obs/root-note.txt"
printf 'old-nested\n' >"$obs/unmanaged/existing.txt"
cat >"$config" <<EOF
[paths]
run_dir = "$run"
socket = "$socket_path"
ready_file = "$run/ready.json"
local_overlay_root = "$run/local-overlays"

[runtime]
socket_mode = "0600"
observe_ready_timeout = 20

[defaults]
write_policy = "shared-nfs"

[ownership]
uid = 0
gid = 0

[[observation_dirs]]
path = "$obs"
state_subdir = ".ccc-storage"
EOF

python - <<'PY'
import pyfuse3  # noqa: F401
import trio  # noqa: F401
print('pyfuse3/trio import ok')
PY

export CCC_MOUNTD_SOCK="$socket_path"
ccc-storage mountd --config "$config" >"$mountd_log" 2>&1 &
mountd_pid="$!"

for _ in $(seq 1 200); do
  if ! kill -0 "$mountd_pid" 2>/dev/null; then
    echo "ccc-storage mountd exited before ready" >&2
    sed -n '1,240p' "$mountd_log" >&2 || true
    exit 1
  fi
  if [ -S "$socket_path" ] && is_mountpoint "$obs"; then
    break
  fi
  sleep 0.1
done
if [ ! -S "$socket_path" ] || ! is_mountpoint "$obs" ]; then
  echo "observation directory did not mount in place" >&2
  sed -n '1,240p' "$mountd_log" >&2 || true
  findmnt -R "$root" >&2 || true
  exit 1
fi

# Existing unmanaged data remains visible through the in-place FUSE root.
test "$(cat "$obs/root-note.txt")" = old-root
test "$(cat "$obs/unmanaged/existing.txt")" = old-nested
if ls -a "$obs" | grep -qx '.ccc-storage'; then
  echo ".ccc-storage leaked into public observation listing" >&2
  exit 1
fi

# Non-mkdir file I/O passes through to the backing NFS source.
printf 'passthrough\n' >"$obs/unmanaged/passthrough.txt"
test "$(cat "$obs/unmanaged/passthrough.txt")" = passthrough
if ! find "$run/observation-sources" -path '*/source/unmanaged/passthrough.txt' -type f -print -quit | grep -q .; then
  echo "unmanaged passthrough write did not land on private source backing" >&2
  find "$run/observation-sources" -maxdepth 6 -type f -print >&2 || true
  exit 1
fi

# Top-level mkdir creates a managed child; the first operation inside the child
# uses a private writable child mount under run/mounts rather than stacking a
# FUSE submount under the public dispatcher path.
mkdir "$obs/env-a"
test -d "$obs/env-a"
printf 'managed payload\n' >"$obs/env-a/created.txt"
sync
if find "$run/observation-sources" -path '*/source/env-a/created.txt' -type f -print -quit | grep -q .; then
  echo "managed child write leaked into backing source" >&2
  exit 1
fi
ccc-storage status env-a --json >"$root/status-env-a-before.json"
python - <<'PY'
import json
from pathlib import Path
root = Path('/ccc-runtime')
data = json.loads((root / 'status-env-a-before.json').read_text())
assert data['mounted'] is True, data
assert '/run/ccc-storage/mounts/' in data['mountpoint'], data
assert data['overlay']['dirty'] is True, data
print('env-a private mount status ok')
PY
ccc-storage umount env-a --json >"$root/umount-env-a.json"
ccc-storage commit env-a -m 'observation-dir smoke env-a' --json >"$root/commit-env-a.json"
python - <<'PY'
import json
from pathlib import Path
root = Path('/ccc-runtime')
data = json.loads((root / 'commit-env-a.json').read_text())
assert data['generation'] == 1, data
assert len(data.get('packs', [])) == 1, data
assert data['overlay']['dirty'] is False, data
print('env-a commit json ok')
PY
for _ in $(seq 1 100); do
  if [ -r "$obs/env-a/created.txt" ]; then
    break
  fi
  ls -la "$obs/env-a" >/dev/null 2>&1 || true
  sleep 0.05
done
test "$(cat "$obs/env-a/created.txt")" = 'managed payload'

ccc-storage doctor --json >"$root/doctor-final.json"
python - <<'PY'
import json
from pathlib import Path
root = Path('/ccc-runtime')
summary = {
    'env_a': json.loads((root / 'commit-env-a.json').read_text())['generation'],
    'observation_dirs': json.loads((root / 'doctor-final.json').read_text())['observation_dirs'],
}
print(json.dumps(summary, indent=2, sort_keys=True))
PY

echo 'observation-dir runtime smoke passed: passthrough, private managed child, commit/remount'
SH
