#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python}"

default_root="${TMPDIR:-/tmp}/ccc-layered-runtime-smoke.$$"
smoke_root="${CCC_SMOKE_ROOT:-$default_root}"

resolve_path() {
  "$python_bin" - "$1" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
}

repo_real="$(resolve_path "$repo_root")"
smoke_real="$(resolve_path "$smoke_root")"

case "$smoke_real" in
  /tmp/*|"$repo_real"/*) ;;
  *)
    echo "refusing unsafe CCC_SMOKE_ROOT: $smoke_real" >&2
    echo "use /tmp or a path inside this repository checkout" >&2
    exit 2
    ;;
esac

nfs_root="$smoke_real/nfs/.ccc-layered"
run_dir="$smoke_real/run"
parent_path="$smoke_real/managed/datasets"
socket_path="$run_dir/mountd.sock"

mountd_pid=""
cleanup() {
  if [ -n "$mountd_pid" ] && kill -0 "$mountd_pid" 2>/dev/null; then
    kill "$mountd_pid" 2>/dev/null || true
    wait "$mountd_pid" 2>/dev/null || true
  fi
  if [ -z "${CCC_SMOKE_KEEP:-}" ]; then
    rm -rf "$smoke_real"
  else
    echo "kept smoke root: $smoke_real"
  fi
}
trap cleanup EXIT

rm -rf "$smoke_real"
mkdir -p "$nfs_root/registry" "$run_dir" "$parent_path"

export PYTHONPATH="$repo_root/src:$repo_root${PYTHONPATH:+:$PYTHONPATH}"
export CCC_NFS_ROOT="$nfs_root"
export CCC_NODE_RUN_DIR="$run_dir"
export CCC_MOUNTD_SOCK="$socket_path"

"$python_bin" -m ccc_layered_mountd.daemon \
  --nfs-root "$nfs_root" \
  --run-dir "$run_dir" \
  --socket "$socket_path" \
  --managed-parent "$parent_path" &
mountd_pid="$!"

for _ in $(seq 1 50); do
  if [ -S "$socket_path" ]; then
    break
  fi
  if ! kill -0 "$mountd_pid" 2>/dev/null; then
    echo "mountd exited before creating socket" >&2
    wait "$mountd_pid"
    exit 1
  fi
  sleep 0.1
done

if [ ! -S "$socket_path" ]; then
  echo "mountd socket was not created: $socket_path" >&2
  exit 1
fi

"$python_bin" -m ccc_layered_cli.main doctor --json >/dev/null
"$python_bin" -m ccc_layered_cli.main create smoke-child --json >/dev/null
"$python_bin" -m ccc_layered_cli.main parent-ls --json | grep -q '"smoke-child"'

echo "runtime smoke passed"
