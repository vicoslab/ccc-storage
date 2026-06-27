#!/usr/bin/env bash
set -euo pipefail

# End-to-end S3 cold-tier and external-HPC exchange smoke.
#
# This validates the integrated path that the simpler S3 smoke does not cover:
# dirty folder data -> ccc-layered commit -> SquashFS delta pack -> S3 cold
# archive -> cold recall -> external-HPC packset/upload/download -> HPC output
# delta import-queue metadata.
#
# Credentials are sourced, never printed.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

endpoint="${CCC_S3_ENDPOINT:-https://ceph-7.fri.uni-lj.si}"
addressing_style="${CCC_S3_ADDRESSING_STYLE:-auto}"
region="${CCC_S3_REGION:-us-east-1}"
credentials_sh="${CCC_S3_CREDENTIALS_SH:-}"
if [ -z "$credentials_sh" ]; then
  if [ -f "$repo_root/../s3_storage_premissions.sh" ]; then
    credentials_sh="$repo_root/../s3_storage_premissions.sh"
  elif [ -f "$repo_root/../s3_storage_premission.sh" ]; then
    credentials_sh="$repo_root/../s3_storage_premission.sh"
  fi
fi

if [ -z "$credentials_sh" ] || [ ! -f "$credentials_sh" ]; then
  echo "S3 credential script not found; set CCC_S3_CREDENTIALS_SH" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
. "$credentials_sh"
set +a

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "credential script did not export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY" >&2
  exit 2
fi

host_safe="$(hostname -s 2>/dev/null || hostname || printf host)"
host_safe="$(printf '%s' "$host_safe" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//')"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_id="${host_safe:-host}-${timestamp}-$$"
work_root="${CCC_S3_COLD_HPC_WORK_ROOT:-$repo_root/.scratch/s3-cold-hpc-smoke-$run_id}"
prefix="${CCC_S3_PREFIX:-ccc-layered-storage/cold-hpc-smoke/$run_id}"

mkdir -p "$work_root"
cleanup() {
  if [ "${CCC_S3_KEEP:-}" = "1" ]; then
    echo "kept S3 cold/HPC smoke scratch: $work_root"
  else
    rm -rf "$work_root"
  fi
}
trap cleanup EXIT

export PYTHONPATH="$repo_root/src:$repo_root${PYTHONPATH:+:$PYTHONPATH}"
python_dir="$($python_bin - <<'PY'
import os
import sys
print(os.path.dirname(os.path.realpath(sys.executable)))
PY
)"
export PATH="$python_dir:$PATH"

"$python_bin" - "$endpoint" "$addressing_style" "$region" "${CCC_S3_BUCKET:-}" "$prefix" "$work_root" <<'PY'
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
import hashlib
from pathlib import Path

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic, load_manifest
from ccc_layered_hpc.hpc_s3_exchange import (
    fetch_hpc_packset_bundle,
    import_hpc_delta_from_s3,
    publish_hpc_import_delta,
    publish_hpc_packset_bundle,
)
from ccc_layered_hpc.importqueue import ImportQueue, Provenance
from ccc_layered_hpc.object_store import Boto3ObjectStore, ObjectStoreError
from ccc_layered_hpc.s3mirror import archive_committed_packs_to_cold_storage, recall_cold_pack
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_pack.bundle import (
    BundleEntry,
    MountGraph,
    MountGraphNode,
    build_packset_bundle,
    unpack_packset_bundle,
)
from ccc_layered_pack.builder import build_pack

endpoint, addressing_style, region, bucket_arg, prefix, work_root_arg = sys.argv[1:]
work_root = Path(work_root_arg)
work_root.mkdir(parents=True, exist_ok=True)

created_bucket = False
bucket = bucket_arg.strip()
if not bucket:
    suffix = uuid.uuid4().hex[:10]
    host = ''.join(ch if ch.isalnum() else '-' for ch in socket.gethostname().lower()).strip('-')[:18]
    bucket = f"ccc-layered-cold-hpc-{host or 'host'}-{int(time.time())}-{suffix}"[:63].strip('-')

store = Boto3ObjectStore(
    bucket=bucket,
    endpoint_url=endpoint,
    region_name=region,
    addressing_style=addressing_style,
)
try:
    if bucket_arg.strip():
        store.ensure_bucket(create=False)
    else:
        store.ensure_bucket(create=True)
        created_bucket = True
except ObjectStoreError as exc:
    raise SystemExit(f"S3 bucket setup failed: {exc}") from exc

nfs_root = work_root / "nfs" / ".ccc-layered"
run_dir = work_root / "run"
source_dir = work_root / "source-folder"
pack_dir = nfs_root / "packs" / "s3-cold-smoke"
registry_dir = nfs_root / "registry"
for path in (pack_dir, registry_dir, run_dir, source_dir):
    path.mkdir(parents=True, exist_ok=True)
(source_dir / "seed.txt").write_text("seed from original folder\n")
(source_dir / "nested").mkdir()
(source_dir / "nested" / "base.txt").write_text("base nested file\n")
base_pack_path = pack_dir / "base-g0001.sqfs"
base = build_pack(source_dir, base_pack_path).pack
manifest = ChildManifest(
    id="dataset:s3-cold-smoke",
    name="s3-cold-smoke",
    type="dataset",
    generation=1,
    pack_stack=PackStack(active_revision="g0001", lowers=(base,)),
)
manifest_path = registry_dir / "s3-cold-smoke.toml"
dump_atomic(manifest_path, manifest)

service = MountdService(nfs_root=nfs_root, run_dir=run_dir)
service.reload_registry()
dirty_rel = "frames/image-001.txt"
active_upper = service.overlay_paths(manifest).active_upper
active_upper.mkdir(parents=True, exist_ok=True)
(active_upper / "frames").mkdir()
(active_upper / dirty_rel).write_text("new image payload from dirty folder\n")
(active_upper / "labels").mkdir()
(active_upper / "labels" / "image-001.txt").write_text("class=smoke\n")
status_before = service.handle_status(manifest.id)
commit = service.handle_commit(manifest.id, message="s3 cold hpc smoke")
committed = load_manifest(manifest_path)
if commit["generation"] != 2 or committed.generation != 2:
    raise SystemExit("commit did not publish generation 2")
if len(committed.pack_stack.lowers) != 2:
    raise SystemExit("commit did not produce base+delta pack stack")
delta_pack = Path(committed.pack_stack.lowers[-1].path)

extract_delta = work_root / "extract-delta"
subprocess.run(["unsquashfs", "-d", str(extract_delta), str(delta_pack)], check=True, stdout=subprocess.DEVNULL)
if (extract_delta / dirty_rel).read_text() != "new image payload from dirty folder\n":
    raise SystemExit("committed delta pack does not contain dirty folder payload")

archive_prefix = f"{prefix.strip('/')}/cold"
archive = archive_committed_packs_to_cold_storage(
    committed,
    manifest_path,
    store,
    prefix=archive_prefix,
    remove_hot=True,
)
if any(Path(pack.path).exists() for pack in committed.pack_stack.lowers):
    raise SystemExit("hot pack file still exists after cold archive")
for pack in committed.pack_stack.lowers:
    key = f"{archive_prefix}/packs/{Path(pack.path).name}"
    actual = hashlib.sha256(store.read_bytes(key)).hexdigest()
    if actual != pack.sha256:
        raise SystemExit(f"S3 object checksum mismatch for {key}")

recalled = recall_cold_pack(load_manifest(manifest_path), manifest_path, store, work_root / "recalled-packs")
if recalled.s3.pack_state != "hot" or len(recalled.pack_stack.lowers) != 2:
    raise SystemExit("cold recall did not restore hot base+delta stack")
recalled_delta = Path(recalled.pack_stack.lowers[-1].path)
extract_recalled = work_root / "extract-recalled-delta"
subprocess.run(["unsquashfs", "-d", str(extract_recalled), str(recalled_delta)], check=True, stdout=subprocess.DEVNULL)
if (extract_recalled / dirty_rel).read_text() != "new image payload from dirty folder\n":
    raise SystemExit("recalled delta pack does not contain dirty folder payload")

graph = MountGraph(
    root=recalled.id,
    included=(MountGraphNode(child_id=recalled.id, path="."),),
    excluded=(MountGraphNode(child_id="private:excluded", path="private", reason="not selected"),),
)
entries = [
    BundleEntry(str(Path(pack.path)), f"packs/{Path(pack.path).name}")
    for pack in recalled.pack_stack.lowers
]
packset = build_packset_bundle(work_root / "hpc-packset.tar", entries, graph)
packset_record = publish_hpc_packset_bundle(
    store,
    packset,
    prefix=f"{prefix.strip('/')}/hpc/input",
    site="validation-only",
    root_id=recalled.id,
    generation=recalled.generation,
)
downloaded_packset = fetch_hpc_packset_bundle(
    store,
    packset_record,
    work_root / "downloaded-hpc-packset.tar",
)
unpacked = unpack_packset_bundle(downloaded_packset, work_root / "unpacked-hpc-packset")
if unpacked.graph.excluded[0].child_id != "private:excluded":
    raise SystemExit("HPC packset mount graph excluded-child metadata was not preserved")

output_delta = work_root / "hpc-output-delta.tar"
output_delta.write_bytes(b"external hpc output delta payload")
provenance = Provenance(
    site="validation-only",
    job_id="slurm-validation-1",
    root_id=recalled.id,
    base_generation=recalled.generation,
)
import_record = publish_hpc_import_delta(
    store,
    output_delta,
    prefix=f"{prefix.strip('/')}/hpc/import/slurm-validation-1",
    branch="hpc-review-slurm-validation-1",
    provenance=provenance,
)
queue = ImportQueue(work_root / "import-queue")
queued = import_hpc_delta_from_s3(
    store,
    f"{prefix.strip('/')}/hpc/import/slurm-validation-1",
    queue,
)
if queued.branch != import_record.branch or queued.provenance != provenance:
    raise SystemExit("S3 HPC import queue metadata did not round-trip")

cleanup_deleted = 0
if os.environ.get("CCC_S3_KEEP") != "1":
    cleanup_deleted = store.delete_prefix(prefix.strip('/') + "/")
    if created_bucket:
        store.client.delete_bucket(Bucket=bucket)

print(json.dumps({
    "endpoint": endpoint,
    "addressing_style": addressing_style,
    "bucket": bucket,
    "created_bucket": created_bucket,
    "prefix": prefix,
    "dirty_file_count_before_commit": status_before["overlay"]["file_count"],
    "committed_generation": committed.generation,
    "committed_pack_count": len(committed.pack_stack.lowers),
    "cold_uploaded_keys": len(archive.uploaded_keys),
    "hot_paths_removed": len(archive.removed_hot_paths),
    "recalled_pack_count": len(recalled.pack_stack.lowers),
    "hpc_packset_key": packset_record.bundle_key,
    "hpc_packset_size": packset_record.size,
    "hpc_import_delta_key": import_record.delta_key,
    "hpc_import_branch": queued.branch,
    "cleanup_deleted_objects": cleanup_deleted,
}, indent=2, sort_keys=True))
PY
