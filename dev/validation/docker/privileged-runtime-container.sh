#!/usr/bin/env bash
set -euo pipefail

# Container-side worker for dev/validation/docker/privileged-runtime-smoke.sh.
# This script is intentionally no sidecar: all mount authority stays inside the
# privileged Docker container.

runtime_root="${CCC_CONTAINER_RUNTIME_ROOT:-/ccc-runtime}"
child_id="${CCC_CHILD_ID:-runtime-child}"
safe_child_id="$(printf '%s' "$child_id" | tr -c 'A-Za-z0-9_.-' '_' | sed 's/^_*//; s/_*$//')"
if [ -z "$safe_child_id" ]; then
  safe_child_id="runtime-child"
fi

seed_dir="$runtime_root/seed"
nfs_root="$runtime_root/nfs"
run_dir="$runtime_root/run"
socket_path="$run_dir/mountd.sock"
pack_dir="$nfs_root/packs/$safe_child_id"
manifest_path="$nfs_root/registry/$safe_child_id.toml"
base_pack="$pack_dir/base-g0001.sqfs"
mount_json="$runtime_root/mount.json"
status_json="$runtime_root/status-before-commit.json"
commit_json="$runtime_root/commit.json"
upperdir="$nfs_root/overlays/$safe_child_id/active"
workdir="$runtime_root/work/$safe_child_id"
view_dir="$runtime_root/views/$safe_child_id"
published_dir="$runtime_root/published/$safe_child_id"
control_dir="$runtime_root/control"
ready_file="$runtime_root/ready"
metadata_file="$runtime_root/metadata.env"
mountd_log="$runtime_root/mountd.log"

mountd_pid=""

cleanup_mounts() {
  set +e
  if mountpoint -q "$published_dir"; then
    umount -l "$published_dir"
  fi
  if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u -z "$view_dir" >/dev/null 2>&1 || true
  fi
  if mountpoint -q "$view_dir"; then
    umount -l "$view_dir"
  fi
  set -e
}

cleanup() {
  set +e
  cleanup_mounts
  if [ -n "$mountd_pid" ] && kill -0 "$mountd_pid" 2>/dev/null; then
    CCC_MOUNTD_SOCK="$socket_path" ccc-layered umount "$child_id" --json >/dev/null 2>&1 || true
    kill "$mountd_pid" >/dev/null 2>&1 || true
    wait "$mountd_pid" >/dev/null 2>&1 || true
  fi
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

mkdir -p \
  "$seed_dir/nested" \
  "$pack_dir" \
  "$(dirname "$manifest_path")" \
  "$run_dir" \
  "$upperdir" \
  "$workdir" \
  "$view_dir" \
  "$published_dir" \
  "$control_dir"

if ! mount --make-rshared "$runtime_root" >/dev/null 2>&1; then
  echo "failed to mark runtime root as rshared inside privileged container: $runtime_root" >&2
  exit 1
fi
if command -v findmnt >/dev/null 2>&1; then
  propagation="$(findmnt -T "$runtime_root" -no PROPAGATION 2>/dev/null || true)"
  case "$propagation" in
    *shared*) ;;
    *)
      echo "runtime root is not shared inside privileged container: $runtime_root propagation=$propagation" >&2
      exit 1
      ;;
  esac
fi

printf 'hello from privileged no-sidecar runtime smoke\n' >"$seed_dir/hello.txt"
printf 'nested seed payload\n' >"$seed_dir/nested/payload.txt"

ccc-pack build \
  "$seed_dir" \
  "$base_pack" \
  --manifest "$manifest_path" \
  --child-id "$child_id" \
  --name "$child_id" \
  --generation 1 \
  --revision g1 \
  --comp gzip \
  --block 128K >/dev/null

export CCC_NFS_ROOT="$nfs_root"
export CCC_NODE_RUN_DIR="$run_dir"
export CCC_MOUNTD_SOCK="$socket_path"

ccc-layered-mountd \
  --nfs-root "$nfs_root" \
  --run-dir "$run_dir" \
  --socket "$socket_path" >"$mountd_log" 2>&1 &
mountd_pid="$!"

for _ in $(seq 1 100); do
  if [ -S "$socket_path" ]; then
    break
  fi
  if ! kill -0 "$mountd_pid" 2>/dev/null; then
    echo "ccc-layered-mountd exited before creating socket" >&2
    sed -n '1,200p' "$mountd_log" >&2 || true
    wait "$mountd_pid"
    exit 1
  fi
  sleep 0.1
done

if [ ! -S "$socket_path" ]; then
  echo "mountd socket was not created: $socket_path" >&2
  sed -n '1,200p' "$mountd_log" >&2 || true
  exit 1
fi

ccc-layered doctor --json >"$runtime_root/doctor.json"
ccc-layered mount "$child_id" --json >"$mount_json"
ccc-layered status "$child_id" --json >"$status_json"

lowerdir="$(python - "$status_json" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
mountpoint = data.get("mountpoint", "")
if not mountpoint:
    raise SystemExit("mounted child status did not include mountpoint")
print(mountpoint)
PY
)"

fuse-overlayfs \
  -o "lowerdir=$lowerdir,upperdir=$upperdir,workdir=$workdir" \
  "$view_dir"

mount --bind "$view_dir" "$published_dir"

if [ "$(cat "$published_dir/hello.txt")" != "hello from privileged no-sidecar runtime smoke" ]; then
  echo "published view did not expose expected seed file inside privileged container" >&2
  exit 1
fi

{
  printf 'CHILD_ID=%q\n' "$child_id"
  printf 'SAFE_CHILD_ID=%q\n' "$safe_child_id"
  printf 'PUBLISHED_PATH=%q\n' "$published_dir"
  printf 'PUBLISHED_REL=%q\n' "published/$safe_child_id"
  printf 'MOUNTD_SOCKET=%q\n' "$socket_path"
  printf 'NFS_ROOT=%q\n' "$nfs_root"
  printf 'LOWERDIR=%q\n' "$lowerdir"
  printf 'UPPERDIR=%q\n' "$upperdir"
  printf 'VIEW_PATH=%q\n' "$view_dir"
} >"$metadata_file"

touch "$ready_file"

while [ ! -e "$control_dir/stop" ]; do
  if [ -e "$control_dir/seal" ]; then
    cleanup_mounts
    mv "$control_dir/seal" "$control_dir/sealed"
  fi
  if ! kill -0 "$mountd_pid" 2>/dev/null; then
    echo "ccc-layered-mountd exited unexpectedly" >&2
    sed -n '1,200p' "$mountd_log" >&2 || true
    exit 1
  fi
  sleep 0.5
done
