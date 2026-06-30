#!/usr/bin/env bash
set -euo pipefail

# Runtime smoke for per-child write policies.
#
# Validates:
#   - shared-nfs policy still writes through NFS fuse-overlayfs upper
#   - local-ssd-async mounts as kernel OverlayFS with local SSD upper/work
#   - explicit per-child policy switching works before mount
#   - local dirty data publishes to NFS mirror and is readable from a second mountd
#   - local write throughput beats shared-NFS dirty writes by a large margin

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image_tag="${CCC_MOUNTD_IMAGE:-ccc-layered-storage-mountd:local}"
app_image="${CCC_APP_IMAGE:-$image_tag}"
runtime_root="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-write-policy-test}"
local_ssd_root="${CCC_LOCAL_SSD_ROOT:-$runtime_root/local-ssd}"
run_id="${CCC_RUNTIME_RUN_ID:-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_root="$runtime_root/runs/$run_id"
ssd_run_root="$local_ssd_root/runs/$run_id"
docker_source_root="${CCC_RUNTIME_DOCKER_SOURCE_ROOT:-}"
docker_ssd_root="${CCC_LOCAL_SSD_DOCKER_SOURCE_ROOT:-}"
keep="${CCC_RUNTIME_KEEP:-0}"
keep_containers="${CCC_RUNTIME_KEEP_CONTAINERS:-0}"
skip_build="${CCC_RUNTIME_SKIP_BUILD:-0}"
timeout_s="${CCC_WRITE_POLICY_TIMEOUT:-240}"
files="${CCC_WRITE_POLICY_FILES:-2000}"
size_kib="${CCC_WRITE_POLICY_SIZE_KIB:-32}"
min_local_fps="${CCC_WRITE_POLICY_MIN_LOCAL_FPS:-4000}"
min_speedup="${CCC_WRITE_POLICY_MIN_SPEEDUP:-5}"
writer_name="ccc-layered-policy-writer-$run_id"
reader_name="ccc-layered-policy-reader-$run_id"
app_name="ccc-layered-policy-app-$run_id"
reader_app_name="ccc-layered-policy-reader-app-$run_id"

docker_bin="${DOCKER:-docker}"

cleanup() {
  if [ "$keep_containers" = "1" ]; then
    echo "kept containers: $app_name $reader_app_name $writer_name $reader_name"
  else
    "$docker_bin" rm -f "$app_name" "$reader_app_name" "$writer_name" "$reader_name" >/dev/null 2>&1 || true
  fi
  if [ "$keep" != "1" ]; then
    rm -rf "$run_root" "$ssd_run_root" 2>/dev/null || true
  else
    echo "kept runtime root: $run_root"
    echo "kept local SSD root: $ssd_run_root"
  fi
}
trap cleanup EXIT

mkdir -p "$run_root"/{nfs,source,published-writer,published-reader,results} "$ssd_run_root"
touch "$run_root/source/CCC_LAYERED_OBSERVE"

if [ "$skip_build" != "1" ]; then
  "$docker_bin" build -f "$repo_root/deploy/docker/mountd.Dockerfile" -t "$image_tag" "$repo_root"
fi

ensure_docker_host_dir() {
  local path=$1
  mkdir -p "$path" 2>/dev/null || true
  "$docker_bin" run --rm --privileged --mount type=bind,src=/,dst=/host "$image_tag" \
    sh -lc "mkdir -p /host$(printf '%q' "$path")"
}

if [ -z "$docker_source_root" ]; then
  docker_run_root="$run_root"
else
  docker_run_root="$docker_source_root/runs/$run_id"
  ensure_docker_host_dir "$docker_run_root"
fi
if [ -z "$docker_ssd_root" ]; then
  docker_ssd_run_root="$ssd_run_root"
else
  docker_ssd_run_root="$docker_ssd_root/runs/$run_id"
  ensure_docker_host_dir "$docker_ssd_run_root"
fi

wait_for_mount() {
  local container=$1
  local target=$2
  local deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$docker_bin" exec "$container" sh -lc \
      "test \"\$(findmnt -T '$target' -no TARGET 2>/dev/null | head -n1)\" = '$target'"; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for $target to be mounted in $container" >&2
  "$docker_bin" logs "$container" >&2 || true
  return 1
}

start_mountd() {
  local name=$1
  local published=$2
  local ssd_suffix=$3
  "$docker_bin" run -d --rm \
    --name "$name" \
    --device /dev/fuse:/dev/fuse:rw \
    --cap-add SYS_ADMIN \
    --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined \
    --mount type=bind,src="$docker_run_root",dst=/ccc-runtime,bind-propagation=rshared \
    --mount type=bind,src="$docker_ssd_run_root",dst=/ccc-ssd,bind-propagation=rshared \
    -e CCC_NFS_ROOT=/ccc-runtime/nfs \
    -e CCC_NODE_RUN_DIR=/run/ccc-layered \
    -e CCC_MOUNTD_SOCK=/run/ccc-layered/mountd.sock \
    -e CCC_OBSERVE_ROOT=/ccc-runtime/source \
    -e CCC_OBSERVE_MOUNTPOINT="/ccc-runtime/$published" \
    -e CCC_LOCAL_OVERLAY_ROOT="/ccc-ssd/$ssd_suffix" \
    -e CCC_DIRTY_PUBLISH_INTERVAL=0.5 \
    -e CCC_MOUNTD_REQUEST_TIMEOUT=300 \
    -e CCC_MOUNTD_SOCKET_MODE=0600 \
    "$image_tag" >/dev/null
  wait_for_mount "$name" "/ccc-runtime/$published"
}

start_app() {
  local name=$1
  local published=$2
  "$docker_bin" run -d --rm \
    --name "$name" \
    --mount type=bind,src="$docker_run_root/$published",dst=/storage/layered,bind-propagation=rslave \
    "$app_image" sh -lc 'while true; do sleep 3600; done' >/dev/null
}

install_perf_writer() {
  local container=$1
  "$docker_bin" exec -i "$container" sh -lc "cat >/tmp/write_perf.py" <<'PY'
from __future__ import annotations
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
files = int(sys.argv[2])
size = int(sys.argv[3]) * 1024
seed = int(sys.argv[4])
rng = random.Random(seed)
payloads = [rng.randbytes(size) for _ in range(files)]
rels = [Path(f"class_{i % 100:03d}") / f"img_{i:06d}.jpg" for i in range(files)]
start = time.perf_counter()
for rel, payload in zip(rels, payloads, strict=True):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
os.sync()
write_seconds = time.perf_counter() - start
h = hashlib.sha256()
start = time.perf_counter()
for rel in rels:
    h.update((root / rel).read_bytes())
read_seconds = time.perf_counter() - start
print(json.dumps({
    "files": files,
    "bytes": files * size,
    "write_seconds": write_seconds,
    "read_seconds": read_seconds,
    "write_files_per_second": files / write_seconds,
    "read_files_per_second": files / read_seconds,
    "sha256": h.hexdigest(),
}))
PY
}

start_mountd "$writer_name" published-writer writer-local
start_app "$app_name" published-writer
install_perf_writer "$app_name"

# Shared-NFS child: created through app mkdir, default policy shared-nfs.
"$docker_bin" exec "$app_name" sh -lc 'mkdir /storage/layered/shared-env'
"$docker_bin" exec "$app_name" sh -lc 'ls -la /storage/layered/shared-env >/dev/null 2>&1 || true'
wait_for_mount "$app_name" /storage/layered/shared-env
shared_fs="$($docker_bin exec "$app_name" sh -lc 'findmnt -T /storage/layered/shared-env -no FSTYPE')"
case "$shared_fs" in fuse.fuse-overlayfs|fuse-overlayfs|fuse.*) ;; *) echo "unexpected shared fs type: $shared_fs" >&2; exit 1 ;; esac
shared_perf="$($docker_bin exec "$app_name" python3 /tmp/write_perf.py /storage/layered/shared-env "$files" "$size_kib" 101)"
if [ ! -e "$run_root/nfs/overlays/observe%3Ashared-env/active/class_000/img_000000.jpg" ]; then
  echo "shared-nfs write did not land in NFS active upper" >&2
  exit 1
fi

# Local child: create manifest, switch policy before first mount, then app access mounts it.
"$docker_bin" exec "$writer_name" ccc-storage observe-mkdir local-env --json >/tmp/ccc-layered-local-create.json
"$docker_bin" exec "$writer_name" ccc-storage write-policy observe:local-env local-ssd-async --json >/tmp/ccc-layered-local-policy.json
"$docker_bin" exec "$app_name" sh -lc 'ls -la /storage/layered/local-env >/dev/null'
wait_for_mount "$app_name" /storage/layered/local-env
local_fs="$($docker_bin exec "$app_name" sh -lc 'findmnt -T /storage/layered/local-env -no FSTYPE')"
if [ "$local_fs" != "overlay" ]; then
  echo "local-ssd-async child is not kernel overlay: $local_fs" >&2
  "$docker_bin" exec "$app_name" sh -lc 'findmnt -T /storage/layered/local-env -o TARGET,FSTYPE,OPTIONS -n' >&2 || true
  exit 1
fi
local_opts="$($docker_bin exec "$app_name" sh -lc 'findmnt -T /storage/layered/local-env -no OPTIONS')"
case "$local_opts" in *upperdir=/ccc-ssd/*) ;; *) echo "local overlay upper is not under /ccc-ssd: $local_opts" >&2; exit 1 ;; esac
local_perf="$($docker_bin exec "$app_name" python3 /tmp/write_perf.py /storage/layered/local-env "$files" "$size_kib" 202)"
if [ -e "$run_root/nfs/overlays/observe%3Alocal-env/active/class_000/img_000000.jpg" ]; then
  echo "local-ssd write leaked into NFS active upper" >&2
  exit 1
fi

"$docker_bin" exec "$writer_name" ccc-storage publish observe:local-env --json >/tmp/ccc-layered-local-publish.json
"$docker_bin" exec "$writer_name" sh -lc 'test -e /ccc-runtime/nfs/async/observe%3Alocal-env/current/class_000/img_000000.jpg' || {
  echo "local async publish did not create NFS mirror" >&2
  exit 1
}

# A second mountd cannot acquire the writer lock, so it must serve latest mirror read-only.
start_mountd "$reader_name" published-reader reader-local
start_app "$reader_app_name" published-reader
"$docker_bin" exec "$reader_app_name" sh -lc 'ls -la /storage/layered/local-env >/dev/null 2>&1 || true'
wait_for_mount "$reader_app_name" /storage/layered/local-env
"$docker_bin" exec "$reader_app_name" sh -lc 'test -s /storage/layered/local-env/class_000/img_000000.jpg'
reader_fs="$($docker_bin exec "$reader_app_name" sh -lc 'findmnt -T /storage/layered/local-env -no FSTYPE')"
case "$reader_fs" in none|overlay) echo "reader unexpectedly got writer-like fs: $reader_fs" >&2; exit 1 ;; *) ;; esac

# Commit after draining writer mount, then verify committed read works through writer app.
"$docker_bin" exec "$writer_name" ccc-storage umount observe:local-env --json >/tmp/ccc-layered-local-umount.json
"$docker_bin" exec "$writer_name" ccc-storage commit observe:local-env --json >/tmp/ccc-layered-local-commit.json
"$docker_bin" exec "$app_name" sh -lc 'ls -la /storage/layered/local-env >/dev/null 2>&1 || true'
wait_for_mount "$app_name" /storage/layered/local-env
"$docker_bin" exec "$app_name" sh -lc 'test -s /storage/layered/local-env/class_000/img_000000.jpg'

"${PYTHON:-python3}" - <<PY
import json
from pathlib import Path
shared = json.loads('''$shared_perf''')
local = json.loads('''$local_perf''')
min_local = float('$min_local_fps')
min_speedup = float('$min_speedup')
local_fps = float(local['write_files_per_second'])
shared_fps = float(shared['write_files_per_second'])
speedup = local_fps / shared_fps if shared_fps else 999.0
if local_fps < min_local and speedup < min_speedup:
    raise SystemExit(f"local write too slow: {local_fps:.2f} files/s, speedup {speedup:.2f}x over shared {shared_fps:.2f}")
commit = json.loads(Path('/tmp/ccc-layered-local-commit.json').read_text())
if commit['generation'] != 1 or len(commit['packs']) != 1:
    raise SystemExit(f"unexpected commit JSON: {commit}")
result = {
    "run_id": "$run_id",
    "files": int('$files'),
    "size_kib": int('$size_kib'),
    "shared_fs": "$shared_fs",
    "local_fs": "$local_fs",
    "reader_fs": "$reader_fs",
    "shared": shared,
    "local": local,
    "local_vs_shared_write_speedup": speedup,
    "commit_generation": commit['generation'],
}
out = Path('$run_root/nfs/results/write-policy-smoke.json')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
print(json.dumps(result, indent=2, sort_keys=True))
PY

printf 'write-policy runtime smoke passed: shared-nfs + local-ssd-async + mirror + commit\n'
