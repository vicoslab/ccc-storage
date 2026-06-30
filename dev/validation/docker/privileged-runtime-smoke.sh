#!/usr/bin/env bash
set -euo pipefail

# Privileged no-sidecar Docker runtime smoke for CCC layered storage.
#
# Environment:
#   CCC_RUNTIME_ROOT            Base scratch root (default:
#                               /storage/user/ccc-layered-storage-runtime-test)
#   CCC_RUNTIME_DOCKER_SOURCE_ROOT
#                               Docker-daemon-visible alias for CCC_RUNTIME_ROOT
#                               (default: resolved CCC_RUNTIME_ROOT)
#   CCC_CLIENT_CONTAINERS       Space-separated existing Docker containers to
#                               exec as runtime clients (default: domen-cuda10)
#   CCC_DOCKER_TAG              Local image tag to build/use (default:
#                               ccc-layered-storage:priv-runtime-local)
#   CCC_SKIP_BUILD              Set to 1 to skip docker build
#   CCC_RUNTIME_KEEP            Set to 1 to keep the per-run root
#   CCC_ALLOW_PROPAGATION_SKIP  Set to 1 to skip host propagation failure

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
tag="${CCC_DOCKER_TAG:-ccc-layered-storage:priv-runtime-local}"
runtime_root_input="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-runtime-test}"
runtime_docker_source_root_was_set=0
if [ "${CCC_RUNTIME_DOCKER_SOURCE_ROOT+x}" = "x" ]; then
  if [ -z "$CCC_RUNTIME_DOCKER_SOURCE_ROOT" ]; then
    echo "refusing empty CCC_RUNTIME_DOCKER_SOURCE_ROOT" >&2
    exit 2
  fi
  runtime_docker_source_root_input="$CCC_RUNTIME_DOCKER_SOURCE_ROOT"
  runtime_docker_source_root_was_set=1
else
  runtime_docker_source_root_input=""
fi
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

validate_runtime_docker_source_root() {
  local root_real="$1"

  case "$root_real" in
    /|/storage|/storage/user|/storage/datasets|/storage/group|/home|/opt|/opt/shared_storage)
      echo "refusing unsafe CCC_RUNTIME_DOCKER_SOURCE_ROOT: $root_real" >&2
      echo "use a specific Docker-daemon-visible subtree for CCC_RUNTIME_DOCKER_SOURCE_ROOT" >&2
      exit 2
      ;;
  esac
}

print_docker_run_failure() {
  local docker_run_output="$1"
  local run_root="$2"
  local docker_run_root_source="$3"
  local runtime_root_real="$4"
  local docker_source_root_real="$5"

  echo "failed to start privileged smoke container" >&2
  if [ -n "$docker_run_output" ]; then
    printf '%s\n' "$docker_run_output" >&2
  fi
  echo "caller-visible run root: $run_root" >&2
  echo "Docker bind source path: $docker_run_root_source" >&2
  echo "resolved CCC_RUNTIME_ROOT: $runtime_root_real" >&2
  echo "resolved CCC_RUNTIME_DOCKER_SOURCE_ROOT: $docker_source_root_real" >&2
  echo "Docker resolves bind source paths on the Docker daemon host; set CCC_RUNTIME_DOCKER_SOURCE_ROOT when the caller path is a container-only alias." >&2
  echo "caller-visible run root listing:" >&2
  ls -ld "$run_root" >&2 || true
  if [ -e "$(dirname "$docker_run_root_source")" ]; then
    echo "Docker source parent listing:" >&2
    ls -ld "$(dirname "$docker_run_root_source")" "$docker_run_root_source" >&2 || true
  else
    echo "docker source path is not visible from caller; assuming shared storage alias" >&2
  fi
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

client_exec_script() {
  local container_name="$1"
  shift
  "$docker_bin" exec -i "$container_name" sh -s -- "$@"
}

request_container_seal() {
  local container_name="$1"
  "$docker_bin" exec "$container_name" sh -lc 'rm -f /ccc-runtime/control/sealed; touch /ccc-runtime/control/seal'
  for _ in $(seq 1 120); do
    if "$docker_bin" exec "$container_name" sh -lc 'test -e /ccc-runtime/control/sealed'; then
      return 0
    fi
    sleep 0.25
  done
  echo "timed out waiting for privileged container to unmount writable view before commit" >&2
  "$docker_bin" logs "$container_name" >&2 || true
  return 1
}

require_docker
require_fuse_device

repo_real="$(resolve_path "$repo_root")"
runtime_root_real="$(resolve_path "$runtime_root_input")"
validate_runtime_root "$runtime_root_real" "$repo_real"
echo "resolved CCC_RUNTIME_ROOT: $runtime_root_real"
if [ "$runtime_docker_source_root_was_set" = "1" ]; then
  docker_source_root_real="$(resolve_path "$runtime_docker_source_root_input")"
else
  docker_source_root_real="$runtime_root_real"
fi
validate_runtime_docker_source_root "$docker_source_root_real"
echo "resolved CCC_RUNTIME_DOCKER_SOURCE_ROOT: $docker_source_root_real"

hostname_safe="$(safe_name "$(hostname -s 2>/dev/null || hostname || printf host)")"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_id="${hostname_safe}-${timestamp}-$$"
run_id_safe="$(safe_name "$run_id")"
run_root="$runtime_root_real/runs/$run_id_safe"
docker_run_root_source="$docker_source_root_real/runs/$run_id_safe"
container_name="ccc-storage-mountd-smoke-$run_id_safe"

if [ "${CCC_SKIP_BUILD:-}" != "1" ]; then
  "$docker_bin" build -t "$tag" "$repo_root"
else
  echo "skipping docker build because CCC_SKIP_BUILD=1"
fi

mkdir -p "$run_root"
echo "per-run root: $run_root"
echo "Docker bind source root: $docker_run_root_source"
if [ "$docker_source_root_real" != "$runtime_root_real" ]; then
  if [ -e "$(dirname "$docker_run_root_source")" ] || [ -e "$docker_source_root_real" ]; then
    if ! mkdir -p "$docker_run_root_source"; then
      echo "failed to create Docker source run root: $docker_run_root_source" >&2
      exit 1
    fi
  else
    echo "docker source path is not visible from caller; assuming shared storage alias"
  fi
fi

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

if ! docker_run_output="$("$docker_bin" run -d \
  --name "$container_name" \
  --privileged \
  --device /dev/fuse:/dev/fuse:rwm \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$docker_run_root_source,dst=/ccc-runtime,bind-propagation=rshared" \
  "$tag" \
  sh -lc 'dev/validation/docker/privileged-runtime-container.sh' 2>&1)"; then
  print_docker_run_failure "$docker_run_output" "$run_root" "$docker_run_root_source" "$runtime_root_real" "$docker_source_root_real"
  exit 1
fi
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
    if ! client_exec_script "$client" "$host_seed" <<'SH'
set -eu
host_seed="$1"
test -r "$host_seed"
test "$(cat "$host_seed")" = 'hello from privileged no-sidecar runtime smoke'
SH
    then
      echo "skip with reason: client container cannot see propagated published seed file: $client path=$host_seed" >&2
      continue
    fi
    client_file="$host_published/client-writes/$client.txt"
    if ! client_exec_script "$client" "$host_published/client-writes" "$client_file" "$client" <<'SH'
set -eu
write_dir="$1"
client_file="$2"
client="$3"
mkdir -p "$write_dir"
printf 'client write from %s\n' "$client" >"$client_file"
SH
    then
      echo "skip with reason: client container could not write to propagated published path: $client path=$client_file" >&2
      continue
    fi
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

request_container_seal "$container_name"

"$docker_bin" exec "$container_name" sh -lc \
  "set -eu; export CCC_NFS_ROOT='$NFS_ROOT' CCC_MOUNTD_SOCK='$MOUNTD_SOCKET'; ccc-storage status '$CHILD_ID' --json >/ccc-runtime/status-after-client.json; ccc-storage commit '$CHILD_ID' -m 'privileged runtime smoke' --json >/ccc-runtime/commit.json"

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

"$docker_bin" exec "$container_name" sh -lc \
  "set -eu
   export CCC_NFS_ROOT='$NFS_ROOT' CCC_MOUNTD_SOCK='$MOUNTD_SOCKET'
   ccc-storage umount '$CHILD_ID' --json >/ccc-runtime/post-commit-umount.json || true
   ccc-storage mount '$CHILD_ID' --json >/ccc-runtime/post-commit-mount.json
   python - /ccc-runtime/post-commit-mount.json <<'PY'
import json
import sys
from pathlib import Path

mount = json.loads(Path(sys.argv[1]).read_text())
mountpoint = Path(mount.get('mountpoint', ''))
if not mountpoint:
    raise SystemExit('post-commit-remount did not return a mountpoint')
base = mountpoint / 'hello.txt'
if base.read_text() != 'hello from privileged no-sidecar runtime smoke\n':
    raise SystemExit(f'post-commit-remount base file mismatch: {base}')
write_dir = mountpoint / 'client-writes'
if not write_dir.is_dir():
    raise SystemExit('post-commit-remount did not expose client-writes delta directory')
written = sorted(path for path in write_dir.glob('*.txt') if path.is_file())
if not written:
    raise SystemExit('post-commit-remount did not expose any delta write files')
for path in written:
    text = path.read_text()
    if 'client write from ' not in text and 'fallback write without external client' not in text:
        raise SystemExit(f'unexpected post-commit-remount delta payload in {path}: {text!r}')
PY
  "

echo "committed stack remount exposed base and delta files"
echo "privileged no-sidecar Docker runtime smoke passed"
