#!/usr/bin/env bash
set -euo pipefail

# Real S3-compatible smoke for CCC layered storage.
#
# This script intentionally sources, but never prints, AWS credentials.
# Environment:
#   CCC_S3_CREDENTIALS_SH   shell file exporting AWS_ACCESS_KEY_ID and
#                           AWS_SECRET_ACCESS_KEY. Default probes:
#                           ../s3_storage_premissions.sh then
#                           ../s3_storage_premission.sh relative to repo root.
#   CCC_S3_ENDPOINT         default: https://ceph-7.fri.uni-lj.si
#   CCC_S3_ADDRESSING_STYLE default: auto
#   CCC_S3_REGION           default: us-east-1
#   CCC_S3_BUCKET           existing bucket to use. If unset, script attempts to
#                           create a temporary bucket and deletes it afterwards.
#   CCC_S3_PREFIX           object prefix. Default is per-run under
#                           ccc-storage/smoke/.
#   CCC_S3_KEEP             set to 1 to keep uploaded objects/scratch output.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
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

# Source credentials without echoing them. Do not enable xtrace in this script.
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
work_root="${CCC_S3_WORK_ROOT:-$repo_root/.scratch/s3-smoke-$run_id}"
prefix="${CCC_S3_PREFIX:-ccc-storage/smoke/$run_id}"

mkdir -p "$work_root"
cleanup() {
  if [ "${CCC_S3_KEEP:-}" = "1" ]; then
    echo "kept S3 smoke scratch: $work_root"
  else
    rm -rf "$work_root"
  fi
}
trap cleanup EXIT

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

"$python_bin" - "$endpoint" "$addressing_style" "$region" "${CCC_S3_BUCKET:-}" "$prefix" "$work_root" <<'PY'
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, S3Info, dump_atomic, load_manifest
from ccc_storage_hpc.object_store import Boto3ObjectStore, ObjectStoreError
from ccc_storage_hpc.s3mirror import RecallError, mirror_committed_packs, recall_cold_pack

endpoint, addressing_style, region, bucket_arg, prefix, work_root_arg = sys.argv[1:]
work_root = Path(work_root_arg)
work_root.mkdir(parents=True, exist_ok=True)

created_bucket = False
bucket = bucket_arg.strip()
if not bucket:
    suffix = uuid.uuid4().hex[:10]
    host = ''.join(ch if ch.isalnum() else '-' for ch in socket.gethostname().lower()).strip('-')[:18]
    bucket = f"ccc-storage-smoke-{host or 'host'}-{int(time.time())}-{suffix}"[:63].strip('-')

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
    if bucket_arg.strip():
        raise SystemExit(f"S3 bucket validation failed for configured bucket: {exc}") from exc
    raise SystemExit(
        "S3 temporary bucket creation failed; set CCC_S3_BUCKET to an existing bucket "
        f"or grant create-bucket permission. Error: {exc}"
    ) from exc

pack_dir = work_root / "packs"
registry_dir = work_root / "registry"
pack_dir.mkdir(parents=True, exist_ok=True)
registry_dir.mkdir(parents=True, exist_ok=True)
pack = pack_dir / "s3-smoke-pack.sqfs"
pack.write_bytes(b"ccc layered storage s3 smoke pack\n")
manifest = ChildManifest(
    id="s3:smoke",
    name="s3-smoke",
    type="dataset",
    generation=1,
    pack_stack=PackStack(
        active_revision="g1",
        lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),),
    ),
)
manifest_path = registry_dir / "s3-smoke.toml"
dump_atomic(manifest_path, manifest)

uploaded = mirror_committed_packs(manifest, manifest_path, store, prefix=prefix)
pack_key = f"{prefix.strip('/')}/packs/{pack.name}"
manifest_key = f"{prefix.strip('/')}/manifest.toml"
if not store.exists(pack_key):
    raise SystemExit(f"uploaded pack object is missing: {pack_key}")
if not store.exists(manifest_key):
    raise SystemExit(f"uploaded manifest object is missing: {manifest_key}")
if store.read_bytes(pack_key) != pack.read_bytes():
    raise SystemExit("uploaded pack readback did not match source bytes")

cold_manifest_path = registry_dir / "s3-smoke-cold.toml"
cold_manifest = replace(manifest, s3=S3Info(pack_state="cold", uri=prefix.strip('/')))
dump_atomic(cold_manifest_path, cold_manifest)
hot_dir = work_root / "hot-recall"
recalled = recall_cold_pack(cold_manifest, cold_manifest_path, store, hot_dir)
recalled_pack = Path(recalled.pack_stack.lowers[0].path)
if recalled.s3.pack_state != "hot":
    raise SystemExit("recall did not mark manifest pack_state hot")
if sha256_file(recalled_pack) != manifest.pack_stack.lowers[0].sha256:
    raise SystemExit("recalled pack checksum mismatch")
if load_manifest(cold_manifest_path).s3.pack_state != "hot":
    raise SystemExit("recall did not persist updated hot manifest")

corrupt_prefix = f"{prefix.strip('/')}-corrupt"
store.put_bytes(f"{corrupt_prefix}/packs/{pack.name}", b"corrupt")
corrupt_manifest_path = registry_dir / "s3-smoke-corrupt.toml"
corrupt_manifest = replace(manifest, s3=S3Info(pack_state="cold", uri=corrupt_prefix))
dump_atomic(corrupt_manifest_path, corrupt_manifest)
try:
    recall_cold_pack(corrupt_manifest, corrupt_manifest_path, store, work_root / "hot-corrupt")
except RecallError:
    corrupt_rejected = True
else:
    corrupt_rejected = False
if not corrupt_rejected:
    raise SystemExit("corrupt recall unexpectedly succeeded")
if (work_root / "hot-corrupt" / pack.name).exists():
    raise SystemExit("corrupt recall published a destination pack")

cleanup_deleted = 0
if os.environ.get("CCC_S3_KEEP") != "1":
    cleanup_deleted += store.delete_prefix(prefix.strip('/') + "/")
    cleanup_deleted += store.delete_prefix(corrupt_prefix.strip('/') + "/")
    if created_bucket:
        try:
            store.client.delete_bucket(Bucket=bucket)
        except Exception as exc:  # cleanup best effort, but report sanitized code/message
            raise SystemExit(f"failed to delete temporary S3 bucket after cleanup: {exc}") from exc

print(json.dumps({
    "endpoint": endpoint,
    "addressing_style": addressing_style,
    "bucket": bucket,
    "created_bucket": created_bucket,
    "prefix": prefix,
    "uploaded_keys": len(uploaded.uploaded_keys),
    "pack_readback_bytes": len(store.read_bytes(pack_key)) if os.environ.get("CCC_S3_KEEP") == "1" else pack.stat().st_size,
    "recalled_sha256": sha256_file(recalled_pack),
    "corrupt_recall_rejected": corrupt_rejected,
    "cleanup_deleted_objects": cleanup_deleted,
}, indent=2, sort_keys=True))
PY
