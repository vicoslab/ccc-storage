#!/usr/bin/env bash
set -euo pipefail

# Real nested SquashFS runtime smoke.
#
# Validates the hierarchical invariant:
#   - parent/root SquashFS contains parent files plus child-boundary stubs only;
#   - nested child payload is stored in a separate child pack namespace;
#   - mountd mounts child packs directly onto parent boundary paths;
#   - the combined tree exposes parent and child data transparently.
#
# Environment:
#   CCC_RUNTIME_ROOT                Base scratch root (default:
#                                   /storage/user/ccc-layered-storage-nested-test)
#   CCC_RUNTIME_DOCKER_SOURCE_ROOT  Docker-daemon-visible alias for runtime root
#   CCC_DOCKER_TAG                  Local image tag (default:
#                                   ccc-layered-storage:nested-runtime-local)
#   CCC_SKIP_BUILD                  Set to 1 to skip docker build
#   CCC_RUNTIME_KEEP                Set to 1 to keep per-run root

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
tag="${CCC_DOCKER_TAG:-ccc-layered-storage:nested-runtime-local}"
runtime_root_input="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-nested-test}"
if [ "${CCC_RUNTIME_DOCKER_SOURCE_ROOT+x}" = "x" ]; then
  runtime_docker_source_root_input="$CCC_RUNTIME_DOCKER_SOURCE_ROOT"
else
  runtime_docker_source_root_input=""
fi

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
  echo "/dev/fuse is not available on the host; nested FUSE smoke cannot run" >&2
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
run_id="${hostname_safe}-${timestamp}-$$"
run_id_safe="$(safe_name "$run_id")"
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
  if [ -e "$(dirname "$docker_run_root_source")" ] || [ -e "$docker_source_root_real" ]; then
    mkdir -p "$docker_run_root_source"
  else
    echo "docker source path is not visible from caller; assuming shared storage alias"
  fi
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
import sys
from pathlib import Path

from ccc_layered_core.manifest import (
    ChildBoundary,
    ChildManifest,
    PackStack,
    dump_atomic,
)
from ccc_layered_mountd.control import ControlServer
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_pack.builder import (
    BOUNDARY_MARKER_NAME,
    build_pack,
    pack_object_dir,
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

parent_id = 'user-root:alice'
child_id = 'conda-env:alice:env-a'
boundary_path = 'conda/envs/env-a'

parent_src = src / 'parent-root'
child_src = src / 'env-a'
(parent_src / 'conda' / 'envs' / 'env-a' / 'bin').mkdir(parents=True)
(parent_src / 'parent-only.txt').write_text('parent-data\n')
(parent_src / 'conda' / 'envs' / 'env-a' / 'bin' / 'python').write_text(
    'child payload leaked into parent pack\n'
)
(child_src / 'bin').mkdir(parents=True)
(child_src / 'lib').mkdir(parents=True)
(child_src / 'bin' / 'python').write_text('child-python\n')
(child_src / 'lib' / 'module.py').write_text('child-module\n')

parent_pack_dir = pack_object_dir(nfs / 'packs', parent_id)
child_pack_dir = pack_object_dir(nfs / 'packs', child_id)
parent_pack_path = parent_pack_dir / 'base.sqfs'
child_pack_path = child_pack_dir / 'base.sqfs'
if parent_pack_dir == child_pack_dir:
    raise SystemExit('parent and nested child pack namespaces are not separated')

# Parent root pack excludes child payload but keeps a boundary stub.
parent_pack = build_pack(parent_src, parent_pack_path, exclude_boundaries=[boundary_path]).pack
# Nested child pack is a separate SquashFS object beside, not inside, the parent namespace.
child_pack = build_pack(child_src, child_pack_path).pack

extract_parent = root / 'extract-parent'
extract(parent_pack_path, extract_parent)  # unsquashfs validates the parent object contents.
if (extract_parent / 'parent-only.txt').read_text() != 'parent-data\n':
    raise SystemExit('parent file missing from parent pack')
marker = extract_parent / boundary_path / BOUNDARY_MARKER_NAME
if not marker.exists():
    raise SystemExit('parent pack did not keep nested boundary marker')
if (extract_parent / boundary_path / 'bin' / 'python').exists():
    raise SystemExit('child payload leaked into parent pack')

parent = ChildManifest(
    id=parent_id,
    name='alice',
    type='user-root',
    generation=1,
    pack_stack=PackStack(active_revision='g1', lowers=(parent_pack,)),
    child_boundaries=(ChildBoundary(path=boundary_path, child_id=child_id),),
)
child = ChildManifest(
    id=child_id,
    name='env-a',
    type='conda-env',
    generation=1,
    parent_id=parent_id,
    parent_path=boundary_path,
    pack_stack=PackStack(active_revision='g1', lowers=(child_pack,)),
)
dump_atomic(nfs / 'registry' / 'roots' / 'alice.toml', parent)
dump_atomic(nfs / 'registry' / 'envs' / 'env-a.toml', child)

sock = root / 'mountd.sock'
service = MountdService(nfs_root=nfs, run_dir=run)
service.reload_registry()
server = ControlServer(sock, service)
server.start()
os.environ['CCC_MOUNTD_SOCK'] = str(sock)
os.environ['CCC_NFS_ROOT'] = str(nfs)
try:
    # Exercise the real socket/CLI path rather than directly calling the service:
    # ccc-storage mount-tree <parent-id> --json
    env = dict(os.environ)
    env['CCC_MOUNTD_SOCK'] = str(sock)
    env['CCC_NFS_ROOT'] = str(nfs)
    cp = subprocess.run(
        ['ccc-storage', 'mount-tree', parent_id, '--json'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if cp.returncode != 0:
        raise SystemExit(
            f'ccc-storage mount-tree failed with code {cp.returncode}: {cp.stderr}{cp.stdout}'
        )
    (root / 'mount-tree.json').write_text(cp.stdout)
    mounted = json.loads(cp.stdout)
    parent_mount = Path(mounted['mountpoint'])
    child_mount = parent_mount / boundary_path
    if (parent_mount / 'parent-only.txt').read_text() != 'parent-data\n':
        raise SystemExit('parent file not visible through nested mounted tree')
    if (child_mount / 'bin' / 'python').read_text() != 'child-python\n':
        raise SystemExit('nested child SquashFS payload not visible at boundary mountpoint: bin/python')
    if (child_mount / BOUNDARY_MARKER_NAME).exists():
        raise SystemExit('parent boundary marker leaked through mounted child view')
    nested = mounted.get('nested_mounts', [])
    if not nested or nested[0].get('mountpoint') != str(child_mount):
        raise SystemExit(f'nested mount metadata missing/wrong: {nested!r}')
finally:
    server.stop()
    service.stop()

print(json.dumps({
    'parent_pack': str(parent_pack_path),
    'child_pack': str(child_pack_path),
    'boundary_path': boundary_path,
    'parent_mountpoint': str(parent_mount),
    'child_mountpoint': str(child_mount),
    'nested_mounts': mounted.get('nested_mounts', []),
}, indent=2, sort_keys=True))
print('nested SquashFS mount exposed parent and child data')
PY
SH
