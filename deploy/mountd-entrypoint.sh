#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

: "${CCC_NFS_ROOT:?CCC_NFS_ROOT is required}"
: "${CCC_OBSERVE_ROOT:?CCC_OBSERVE_ROOT is required}"
: "${CCC_OBSERVE_MOUNTPOINT:?CCC_OBSERVE_MOUNTPOINT is required}"

CCC_NODE_RUN_DIR="${CCC_NODE_RUN_DIR:-/run/ccc-layered}"
CCC_MOUNTD_SOCK="${CCC_MOUNTD_SOCK:-${CCC_NODE_RUN_DIR}/mountd.sock}"
CCC_MOUNTD_SOCKET_MODE="${CCC_MOUNTD_SOCKET_MODE:-0600}"
CCC_MOUNTD_READY_FILE="${CCC_MOUNTD_READY_FILE:-${CCC_NODE_RUN_DIR}/ready.json}"
CCC_OBSERVE_READY_TIMEOUT="${CCC_OBSERVE_READY_TIMEOUT:-10}"
CCC_IDLE_UNMOUNT_TTL="${CCC_IDLE_UNMOUNT_TTL:-300}"
CCC_IDLE_REAP_INTERVAL="${CCC_IDLE_REAP_INTERVAL:-30}"
CCC_MOUNTD_EXTRA_ARGS="${CCC_MOUNTD_EXTRA_ARGS:-}"

mkdir -p "$CCC_NFS_ROOT" "$CCC_NODE_RUN_DIR" "$CCC_OBSERVE_ROOT" "$CCC_OBSERVE_MOUNTPOINT"

if [ ! -c /dev/fuse ]; then
  echo "ccc-layered-mountd: /dev/fuse is not available as a character device" >&2
  exit 2
fi

for bin in mksquashfs unsquashfs squashfuse fuse-overlayfs fusermount3; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ccc-layered-mountd: required runtime binary missing: $bin" >&2
    exit 2
  fi
done

if [ -n "${CCC_PROPAGATION_CHECK_PATH:-}" ] && command -v findmnt >/dev/null 2>&1; then
  propagation="$(findmnt -T "$CCC_PROPAGATION_CHECK_PATH" -no PROPAGATION 2>/dev/null || true)"
  case "$propagation" in
    shared|slave) ;;
    *)
      echo "ccc-layered-mountd: $CCC_PROPAGATION_CHECK_PATH propagation is '${propagation:-unknown}', expected shared/slave" >&2
      exit 2
      ;;
  esac
fi

exec ccc-layered-mountd \
  --nfs-root "$CCC_NFS_ROOT" \
  --run-dir "$CCC_NODE_RUN_DIR" \
  --socket "$CCC_MOUNTD_SOCK" \
  --socket-mode "$CCC_MOUNTD_SOCKET_MODE" \
  --ready-file "$CCC_MOUNTD_READY_FILE" \
  --observe-ready-timeout "$CCC_OBSERVE_READY_TIMEOUT" \
  --idle-unmount-ttl "$CCC_IDLE_UNMOUNT_TTL" \
  --idle-reap-interval "$CCC_IDLE_REAP_INTERVAL" \
  --observe-root "$CCC_OBSERVE_ROOT" \
  --observe-mountpoint "$CCC_OBSERVE_MOUNTPOINT" \
  $CCC_MOUNTD_EXTRA_ARGS
