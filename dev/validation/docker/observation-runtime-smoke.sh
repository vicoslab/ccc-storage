#!/usr/bin/env bash
set -euo pipefail

# Real marker-observation SquashFS runtime smoke.
#
# Validates:
#   - CCC_LAYERED_OBSERVE at the root makes top-level dirs child boundaries;
#   - a nested CCC_LAYERED_OBSERVE creates deeper independent child boundaries;
#   - parent packs keep marker files and mountpoint stubs, but exclude child payload;
#   - child packs live in separate pack_object_dir namespaces;
#   - observation registration is lazy: no child mounts before access, only the
#     accessed observed child mounts after access.

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
tag="${CCC_DOCKER_TAG:-ccc-layered-storage:observation-runtime-local}"
runtime_root_input="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-observation-test}"
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
  echo "/dev/fuse is not available on the host; observation FUSE smoke cannot run" >&2
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
  mkdir -p "$docker_run_root_source" 2>/dev/null || true
fi

echo "per-run root: $run_root"
echo "Docker bind source root: $docker_run_root_source"

"$docker_bin" run --rm -i \
  --privileged \
  --device /dev/fuse:/dev/fuse:rwm \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$docker_run_root_source,dst=/ccc-runtime,bind-propagation=rshared" \
  "$tag" \
  sh -s <<'SH'
set -euo pipefail
python - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from ccc_layered_core.manifest import PackStack, dump_atomic, load_manifest
from ccc_layered_core.observe import OBSERVE_MARKER_NAME, immediate_child_boundaries
from ccc_layered_mountd.control import ControlServer
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_pack.builder import (
    BOUNDARY_MARKER_NAME,
    build_pack,
    pack_object_dir,
    safe_pack_name,
)
from ccc_layered_pack.reader import extract

root = Path('/ccc-runtime')
nfs = root / 'nfs'
run = root / 'run'
src = root / 'src'
for name in ('registry', 'packs', 'overlays', 'locks'):
    (nfs / name).mkdir(parents=True, exist_ok=True)
run.mkdir(parents=True, exist_ok=True)
src.mkdir(parents=True, exist_ok=True)

(src / OBSERVE_MARKER_NAME).write_text('')
(src / 'root-only.txt').write_text('root\n')
(src / 'user1' / 'profile.txt').parent.mkdir(parents=True)
(src / 'user1' / 'profile.txt').write_text('user1\n')
(src / 'user2' / 'profile.txt').parent.mkdir(parents=True)
(src / 'user2' / 'profile.txt').write_text('user2\n')
conda = src / 'user1' / 'conda'
conda.mkdir(parents=True, exist_ok=True)
(conda / OBSERVE_MARKER_NAME).write_text('')
(conda / 'env-a' / 'bin').mkdir(parents=True)
(conda / 'env-a' / 'bin' / 'python').write_text('env-a-python\n')

boundaries = immediate_child_boundaries(src)
expected = ('user1', 'user2', 'user1/conda/env-a')
if boundaries != expected:
    raise SystemExit(f'observed boundaries mismatch: {boundaries!r}')

root_pack_dir = pack_object_dir(nfs / 'packs', 'observe-root')
user1_pack_dir = pack_object_dir(nfs / 'packs', 'observe:user1')
user2_pack_dir = pack_object_dir(nfs / 'packs', 'observe:user2')
env_pack_dir = pack_object_dir(nfs / 'packs', 'observe:user1/conda/env-a')
if len({root_pack_dir, user1_pack_dir, user2_pack_dir, env_pack_dir}) != 4:
    raise SystemExit('observed pack namespaces are not separated')

root_pack = build_pack(src, root_pack_dir / 'base.sqfs', exclude_observed=True).pack
user1_pack = build_pack(src / 'user1', user1_pack_dir / 'base.sqfs', exclude_observed=True).pack
user2_pack = build_pack(src / 'user2', user2_pack_dir / 'base.sqfs').pack
env_pack = build_pack(src / 'user1' / 'conda' / 'env-a', env_pack_dir / 'base.sqfs').pack

extract_root = root / 'extract-root'
extract_user1 = root / 'extract-user1'
extract(root_pack.path, extract_root)
extract(user1_pack.path, extract_user1)
if not (extract_root / OBSERVE_MARKER_NAME).exists():
    raise SystemExit('root observation marker missing from root pack')
if not (extract_root / 'user1' / BOUNDARY_MARKER_NAME).exists():
    raise SystemExit('root pack missing user1 mountpoint stub')
if (extract_root / 'user1' / 'profile.txt').exists():
    raise SystemExit('observed user1 payload leaked into root pack')
if not (extract_user1 / 'conda' / OBSERVE_MARKER_NAME).exists():
    raise SystemExit('nested observation marker missing from user1 pack')
if not (extract_user1 / 'conda' / 'env-a' / BOUNDARY_MARKER_NAME).exists():
    raise SystemExit('user1 pack missing env-a mountpoint stub')
if (extract_user1 / 'conda' / 'env-a' / 'bin' / 'python').exists():
    raise SystemExit('observed env-a payload leaked into user1 pack')

service = MountdService(nfs_root=nfs, run_dir=run, observe_root=src)
for rel in ('user1', 'user2', 'user1/conda/env-a'):
    service.handle_observe_mkdir(rel)
manifest_specs = {
    'user1': user1_pack,
    'user2': user2_pack,
    'user1/conda/env-a': env_pack,
}
for rel, pack in manifest_specs.items():
    path = nfs / 'registry' / 'observe' / f"{safe_pack_name(rel)}.toml"
    manifest = load_manifest(path)
    dump_atomic(
        path,
        replace(
            manifest,
            generation=1,
            pack_stack=PackStack(active_revision='g1', lowers=(pack,)),
        ),
    )
if service.mounts.active_count() != 0:
    raise SystemExit('lazy observation mounted children before access')

sock = root / 'mountd.sock'
server = ControlServer(sock, service)
server.start()
try:
    env = dict(os.environ)
    env['CCC_MOUNTD_SOCK'] = str(sock)
    cp_user2 = subprocess.run(
        ['ccc-layered', 'observe-access', 'user2/profile.txt', '--json'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if cp_user2.returncode != 0:
        raise SystemExit(f'ccc-layered observe-access user2 failed: {cp_user2.stderr}{cp_user2.stdout}')
    mounted_user2 = json.loads(cp_user2.stdout)
    active = service.mounts.active_ids()
    if active != ['observe:user2']:
        raise SystemExit(f'expected only user2 mounted after first access, got {active!r}')
    user2_mountpoint = Path(mounted_user2['mountpoint'])
    if (user2_mountpoint / 'profile.txt').read_text() != 'user2\n':
        raise SystemExit('observed user2 SquashFS payload not visible after lazy access')

    cp_umount = subprocess.run(
        ['ccc-layered', 'umount', 'observe:user2', '--json'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if cp_umount.returncode != 0:
        raise SystemExit(f'ccc-layered umount user2 failed: {cp_umount.stderr}{cp_umount.stdout}')
    if service.mounts.active_count() != 0:
        raise SystemExit(f'user2 remained mounted after unmount: {service.mounts.active_ids()!r}')

    cp = subprocess.run(
        ['ccc-layered', 'observe-access', 'user1/conda/env-a/bin/python', '--json'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if cp.returncode != 0:
        raise SystemExit(f'ccc-layered observe-access failed: {cp.stderr}{cp.stdout}')
    mounted = json.loads(cp.stdout)
    active = service.mounts.active_ids()
    if active != ['observe:user1/conda/env-a']:
        raise SystemExit(f'expected only env-a mounted after access, got {active!r}')
    mountpoint = Path(mounted['mountpoint'])
    if (mountpoint / 'bin' / 'python').read_text() != 'env-a-python\n':
        raise SystemExit('observed env-a SquashFS payload not visible after lazy access')
finally:
    server.stop()
    service.stop()

print(json.dumps({
    'boundaries': boundaries,
    'root_pack': str(root_pack.path),
    'user1_pack': str(user1_pack.path),
    'user2_pack': str(user2_pack.path),
    'env_pack': str(env_pack.path),
    'mounted_after_access': active,
}, indent=2, sort_keys=True))
print('marker observation runtime smoke passed: lazy access mounted only requested child')
PY
SH
