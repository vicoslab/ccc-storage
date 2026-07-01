#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
python_bin="${PYTHON:-python}"

# Default inside the repo so CCC's fusermount3 sidecar shim sees the mountpoint
# under the container's shared workspace bind. Plain hosts may still override
# CCC_SMOKE_ROOT=/tmp/ccc-storage-fuse-smoke.$$ if their normal fusermount3
# permits /tmp mountpoints.
default_root="$repo_root/.scratch/ccc-storage-fuse-smoke.$$"
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

src_dir="$smoke_real/src"
pack_path="$smoke_real/pack/smoke.sqfs"
extract_dir="$smoke_real/extract"
mount_dir="$smoke_real/mount"
expected_text="hello from ccc layered fuse smoke"

cleanup() {
  if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u "$mount_dir" 2>/dev/null || true
  fi
  if command -v umount >/dev/null 2>&1; then
    umount "$mount_dir" 2>/dev/null || true
  fi
  if [ -z "${CCC_SMOKE_KEEP:-}" ]; then
    rm -rf "$smoke_real"
  else
    echo "kept smoke root: $smoke_real"
  fi
}
trap cleanup EXIT

rm -rf "$smoke_real"
mkdir -p "$src_dir/nested" "$(dirname "$pack_path")" "$mount_dir"
printf '%s\n' "$expected_text" >"$src_dir/hello.txt"
printf 'nested payload\n' >"$src_dir/nested/payload.txt"

export PYTHONPATH="$repo_root/src:$repo_root${PYTHONPATH:+:$PYTHONPATH}"

"$python_bin" -m ccc_storage_pack.cli build \
  "$src_dir" \
  "$pack_path" \
  --comp gzip \
  --block 128K >/dev/null

"$python_bin" -m ccc_storage_pack.cli verify "$pack_path" >/dev/null

if ! command -v unsquashfs >/dev/null 2>&1; then
  echo "unsquashfs not found; install squashfs-tools for extraction validation" >&2
  exit 2
fi

unsquashfs -f -d "$extract_dir" "$pack_path" >/dev/null
if [ "$(cat "$extract_dir/hello.txt")" != "$expected_text" ]; then
  echo "extracted SquashFS payload did not match expected content" >&2
  exit 1
fi

fuse_reason=""
if ! command -v squashfuse >/dev/null 2>&1; then
  fuse_reason="squashfuse not found"
elif ! command -v fusermount3 >/dev/null 2>&1; then
  fuse_reason="fusermount3 not found"
elif [ ! -c /dev/fuse ]; then
  fuse_reason="/dev/fuse is not available"
elif [ ! -r /dev/fuse ] || [ ! -w /dev/fuse ]; then
  fuse_reason="/dev/fuse is not readable and writable by the current user"
fi

skip_or_fail_mount() {
  reason="$1"
  if [ "${CCC_ALLOW_FUSE_SKIP:-}" = "1" ]; then
    echo "skip with reason: FUSE mount skipped: $reason"
    echo "fuse smoke passed without mount"
    exit 0
  fi
  echo "FUSE mount unavailable: $reason" >&2
  echo "set CCC_ALLOW_FUSE_SKIP=1 to validate build/verify/extract only" >&2
  exit 3
}

if [ -n "$fuse_reason" ]; then
  skip_or_fail_mount "$fuse_reason"
fi

mount_log="$smoke_real/squashfuse.log"
if ! squashfuse -o ro "$pack_path" "$mount_dir" >"$mount_log" 2>&1; then
  reason="$(tr '\n' ' ' <"$mount_log" | sed 's/[[:space:]]*$//')"
  skip_or_fail_mount "squashfuse failed${reason:+: $reason}"
fi

for _ in $(seq 1 50); do
  if [ -f "$mount_dir/hello.txt" ]; then
    break
  fi
  sleep 0.1
done

if [ ! -f "$mount_dir/hello.txt" ]; then
  echo "mounted SquashFS payload did not expose hello.txt" >&2
  exit 1
fi

if [ "$(cat "$mount_dir/hello.txt")" != "$expected_text" ]; then
  echo "mounted SquashFS payload did not match expected content" >&2
  exit 1
fi

echo "fuse smoke passed"
