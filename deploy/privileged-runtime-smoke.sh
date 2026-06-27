#!/usr/bin/env bash
set -euo pipefail

# Privileged no-sidecar Docker runtime smoke for CCC layered storage.
#
# Environment:
#   CCC_RUNTIME_ROOT            Base scratch root (default:
#                               /storage/user/ccc-layered-storage-runtime-test)
#   CCC_CLIENT_CONTAINERS       Space-separated existing Docker containers to
#                               exec as runtime clients (default: domen-cuda10)
#   CCC_DOCKER_TAG              Local image tag to build/use (default:
#                               ccc-layered-storage:priv-runtime-local)
#   CCC_SKIP_BUILD              Set to 1 to skip docker build
#   CCC_RUNTIME_KEEP            Set to 1 to keep the per-run root
#   CCC_ALLOW_PROPAGATION_SKIP  Set to 1 to skip host propagation failure

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_bin="${DOCKER:-docker}"
python_bin="${PYTHON:-python}"
tag="${CCC_DOCKER_TAG:-ccc-layered-storage:priv-runtime-local}"
runtime_root_input="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-runtime-test}"
client_containers="${CCC_CLIENT_CONTAINERS:-domen-cuda10}"

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

require_docker() {
  if ! command -v "$docker_bin" >/dev/null 2>&1; then
    echo "docker not found; install Docker or set DOCKER=/path/to/docker" >&2
    exit 2
  fi
}

require_fuse_device() {
  if [ ! -c /dev/fuse ]; then
    echo "/dev/fuse is not available on the host; privileged Docker FUSE smoke cannot run" >&2
    exit 3
  fi
}

validate_runtime_root() {
  local root_real="$1"
  local repo_real="$2"

  case "$root_real" in
    /|/storage|/storage/user|/storage/datasets|/storage/group|/home|/tmp|"$repo_real"|"$repo_real/.scratch")
      echo "refusing unsafe CCC_RUNTIME_ROOT: $root_real" >&2
      echo "use /storage/user/*, /tmp/*, or this checkout's .scratch/*" >&2
      exit 2
      ;;
  esac

  case "$root_real" in
    /storage/user/*|/tmp/*|"$repo_real"/.scratch/*) ;;
    *)
      echo "refusing unsafe CCC_RUNTIME_ROOT: $root_real" >&2
      echo "use /storage/user/*, /tmp/*, or this checkout's .scratch/*" >&2
      exit 2
      ;;
  esac
}

wait_for_ready() {
  local container_name="$1"
  local run_root="$2"

  for _ in $(seq 1 240); do
    if [ -f "$run_root/ready" ] && [ -f "$run_root/metadata.env" ]; then
      return 0
    fi
    if ! "$docker_bin" inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null | grep -q true; then
      echo "privileged smoke container exited before ready" >&2
      "$docker_bin" logs "$container_name" >&2 || true
      return 1
    fi
    sleep 0.25
  done

  echo "timed out waiting for privileged smoke container readiness" >&2
  "$docker_bin" logs "$container_name" >&2 || true
  return 1
}

print_propagation_failure() {
  local host_file="$1"
  local run_root="$2"

  echo "mount propagation failure: host cannot read $host_file" >&2
  echo "container created a bind-published view, but it did not propagate through $run_root" >&2
  echo "findmnt for run root:" >&2
  findmnt -T "$run_root" -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION >&2 || true
  echo "findmnt for expected file parent:" >&2
  findmnt -T "$(dirname "$host_file")" -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION >&2 || true
}

cleanup_container() {
  local container_name="${1:-}"
  if [ -n "$container_name" ] && "$docker_bin" inspect "$container_name" >/dev/null 2>&1; then
    "$docker_bin" exec "$container_name" sh -lc 'touch /ccc-runtime/control/stop' >/dev/null 2>&1 || true
    "$docker_bin" stop "$container_name" >/dev/null 2>&1 || true
    "$docker_bin" rm "$container_name" >/dev/null 2>&1 || true
  fi
}

require_docker
require_fuse_device

repo_real="$(resolve_path "$repo_root")"
runtime_root_real="$(resolve_path "$runtime_root_input")"
validate_runtime_root "$runtime_root_real" "$repo_real"
echo "resolved CCC_RUNTIME_ROOT: $runtime_root_real"

hostname_safe="$(safe_name "$(hostname -s 2>/dev/null || hostname || printf host)")"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_id="${hostname_safe}-${timestamp}-$$"
run_id_safe="$(safe_name "$run_id")"
run_root="$runtime_root_real/runs/$run_id_safe"
container_name="ccc-layered-mountd-smoke-$run_id_safe"

if [ "${CCC_SKIP_BUILD:-}" != "1" ]; then
  "$docker_bin" build -t "$tag" "$repo_root"
else
  echo "skipping docker build because CCC_SKIP_BUILD=1"
fi

mkdir -p "$run_root"
echo "per-run root: $run_root"

container_started=""
cleanup() {
  cleanup_container "$container_started"
  if [ "${CCC_RUNTIME_KEEP:-}" = "1" ]; then
    echo "kept runtime root: $run_root"
  else
    rm -rf "$run_root"
  fi
}
trap cleanup EXIT

"$docker_bin" run -d \
  --name "$container_name" \
  --privileged \
  --device /dev/fuse:/dev/fuse:rwm \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$run_root,dst=/ccc-runtime,bind-propagation=rshared" \
  "$tag" \
  sh -lc 'deploy/privileged-runtime-container.sh' >/dev/null
container_started="$container_name"

wait_for_ready "$container_name" "$run_root"

# shellcheck disable=SC1091
. "$run_root/metadata.env"

host_published="$run_root/$PUBLISHED_REL"
host_seed="$host_published/hello.txt"
propagation_ok=1

if [ ! -r "$host_seed" ]; then
  print_propagation_failure "$host_seed" "$run_root"
  if [ "${CCC_ALLOW_PROPAGATION_SKIP:-}" != "1" ]; then
    exit 1
  fi
  propagation_ok=0
  echo "skip with reason: propagation failure ignored because CCC_ALLOW_PROPAGATION_SKIP=1"
else
  seed_text="$(cat "$host_seed")"
  if [ "$seed_text" != "hello from privileged no-sidecar runtime smoke" ]; then
    echo "unexpected host seed payload: $seed_text" >&2
    exit 1
  fi
  echo "host can read propagated published seed file: $host_seed"
fi

client_write_count=0
written_clients=""
if [ -z "$client_containers" ]; then
  echo "skip with reason: CCC_CLIENT_CONTAINERS is empty"
elif [ "$propagation_ok" != "1" ]; then
  echo "skip with reason: client-container checks require a propagated published path"
elif [ "${runtime_root_real#/storage/user/}" = "$runtime_root_real" ]; then
  echo "skip with reason: client-container checks require CCC_RUNTIME_ROOT under /storage/user"
else
  for client in $client_containers; do
    if ! "$docker_bin" inspect "$client" >/dev/null 2>&1; then
      echo "skip with reason: client container does not exist: $client"
      continue
    fi
    if ! "$docker_bin" inspect -f '{{.State.Running}}' "$client" 2>/dev/null | grep -q true; then
      echo "skip with reason: client container is not running: $client"
      continue
    fi
    client_file="$host_published/client-writes/$client.txt"
    "$docker_bin" exec "$client" sh -lc \
      "set -eu; test \"\$(cat '$host_seed')\" = 'hello from privileged no-sidecar runtime smoke'; mkdir -p '$host_published/client-writes'; printf 'client write from %s\n' '$client' >'$client_file'"
    client_write_count=$((client_write_count + 1))
    written_clients="$written_clients $client"
  done
fi

if [ "$client_write_count" -gt 0 ]; then
  for client in $written_clients; do
    client_file="$host_published/client-writes/$client.txt"
    upper_file="$run_root/nfs/overlays/$SAFE_CHILD_ID/active/client-writes/$client.txt"
    if [ -f "$client_file" ] || [ -f "$upper_file" ]; then
      continue
    fi
    echo "client write was not visible on host or overlay upper for $client" >&2
    exit 1
  done
else
  "$docker_bin" exec "$container_name" sh -lc \
    "set -eu; mkdir -p '/ccc-runtime/published/$SAFE_CHILD_ID/client-writes'; printf 'fallback write without external client\n' >'/ccc-runtime/published/$SAFE_CHILD_ID/client-writes/no-client.txt'"
fi

"$docker_bin" exec "$container_name" sh -lc \
  "set -eu; export CCC_NFS_ROOT='$NFS_ROOT' CCC_MOUNTD_SOCK='$MOUNTD_SOCKET'; ccc-layered status '$CHILD_ID' --json >/ccc-runtime/status-after-client.json; ccc-layered commit '$CHILD_ID' -m 'privileged runtime smoke' --json >/ccc-runtime/commit.json"

generation="$("$python_bin" - "$run_root/commit.json" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
print(data["generation"])
PY
)"

if [ "$generation" -lt 2 ]; then
  echo "commit did not increment manifest generation; got $generation" >&2
  exit 1
fi

if ! find "$run_root/nfs/packs/$SAFE_CHILD_ID" -name 'delta-g*.sqfs' -type f -print -quit | grep -q .; then
  echo "commit did not produce a delta pack under $run_root/nfs/packs/$SAFE_CHILD_ID" >&2
  exit 1
fi

echo "privileged no-sidecar Docker runtime smoke passed"
