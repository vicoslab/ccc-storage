#!/usr/bin/env bash
set -euo pipefail

# Full CCC layered storage write/read performance validation.
#
# Compares, in the same Docker/FUSE runtime:
#   1. direct node-local SSD bind
#   2. direct NFS bind
#   3. ccc-layered shared-nfs child (fuse-overlayfs with NFS upper/work)
#   4. ccc-layered local-ssd-async child (kernel OverlayFS with local upper/work)
#
# Default workloads intentionally cover both requested shapes:
#   - image-small: thousands of image-like files, 500 KiB-class payloads
#   - large-files: few files, each >100 MiB

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image_tag="${CCC_MOUNTD_IMAGE:-ccc-layered-mountd:local}"
app_image="${CCC_APP_IMAGE:-$image_tag}"
runtime_root="${CCC_RUNTIME_ROOT:-/storage/user/ccc-layered-storage-performance-test}"
local_ssd_root="${CCC_LOCAL_SSD_ROOT:-/tmp/ccc-layered-storage-performance-local}"
run_id="${CCC_RUNTIME_RUN_ID:-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_root="$runtime_root/runs/$run_id"
ssd_run_root="$local_ssd_root/runs/$run_id"
docker_source_root="${CCC_RUNTIME_DOCKER_SOURCE_ROOT:-}"
docker_ssd_root="${CCC_LOCAL_SSD_DOCKER_SOURCE_ROOT:-}"
keep="${CCC_RUNTIME_KEEP:-1}"
keep_containers="${CCC_RUNTIME_KEEP_CONTAINERS:-0}"
skip_build="${CCC_RUNTIME_SKIP_BUILD:-0}"
timeout_s="${CCC_PERF_TIMEOUT:-600}"
small_files="${CCC_PERF_SMALL_FILES:-2000}"
small_size_kib="${CCC_PERF_SMALL_SIZE_KIB:-512}"
large_files="${CCC_PERF_LARGE_FILES:-4}"
large_size_mib="${CCC_PERF_LARGE_SIZE_MIB:-128}"
min_local_async_speedup="${CCC_PERF_MIN_LOCAL_ASYNC_SPEEDUP:-1.0}"
min_direct_local_speedup="${CCC_PERF_MIN_DIRECT_LOCAL_SPEEDUP:-0.0}"
writer_name="ccc-layered-perf-writer-$run_id"
app_name="ccc-layered-perf-app-$run_id"
docker_bin="${DOCKER:-docker}"

cleanup() {
  if [ "$keep_containers" = "1" ]; then
    echo "kept containers: $app_name $writer_name"
  else
    "$docker_bin" rm -f "$app_name" "$writer_name" >/dev/null 2>&1 || true
  fi
  if [ "$keep" != "1" ]; then
    rm -rf "$run_root" "$ssd_run_root" 2>/dev/null || true
  else
    echo "kept runtime root: $run_root"
    echo "kept local SSD root: $ssd_run_root"
  fi
}
trap cleanup EXIT

mkdir -p \
  "$run_root"/{nfs,source,published,results} \
  "$run_root/nfs"/{direct,results} \
  "$ssd_run_root"/{direct,local-overlays}
touch "$run_root/source/CCC_LAYERED_OBSERVE"

if [ "$skip_build" != "1" ]; then
  "$docker_bin" build -f "$repo_root/deploy/docker/mountd.Dockerfile" -t "$image_tag" "$repo_root"
fi

ensure_docker_host_dir() {
  local path=$1
  mkdir -p "$path" 2>/dev/null || true
  "$docker_bin" run --rm --privileged --mount type=bind,src=/,dst=/host "$image_tag" \
    sh -lc "mkdir -p /host$(printf '%q' "$path")"
}

if [ -z "$docker_source_root" ]; then
  docker_run_root="$run_root"
else
  docker_run_root="$docker_source_root/runs/$run_id"
  ensure_docker_host_dir "$docker_run_root"
fi
if [ -z "$docker_ssd_root" ]; then
  docker_ssd_run_root="$ssd_run_root"
else
  docker_ssd_run_root="$docker_ssd_root/runs/$run_id"
  ensure_docker_host_dir "$docker_ssd_run_root"
fi
ensure_docker_host_dir "$docker_run_root/nfs/direct"
ensure_docker_host_dir "$docker_run_root/nfs/results"
ensure_docker_host_dir "$docker_run_root/source"
ensure_docker_host_dir "$docker_run_root/published"
ensure_docker_host_dir "$docker_ssd_run_root/direct"
ensure_docker_host_dir "$docker_ssd_run_root/local-overlays"

wait_for_mount() {
  local container=$1
  local target=$2
  local deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$docker_bin" exec "$container" sh -lc \
      "test \"\$(findmnt -T '$target' -no TARGET 2>/dev/null | head -n1)\" = '$target'"; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for $target to be mounted in $container" >&2
  "$docker_bin" logs "$container" >&2 || true
  return 1
}

start_mountd() {
  "$docker_bin" run -d --rm \
    --name "$writer_name" \
    --device /dev/fuse:/dev/fuse:rw \
    --cap-add SYS_ADMIN \
    --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined \
    --mount type=bind,src="$docker_run_root",dst=/ccc-runtime,bind-propagation=rshared \
    --mount type=bind,src="$docker_ssd_run_root",dst=/ccc-ssd,bind-propagation=rshared \
    -e CCC_NFS_ROOT=/ccc-runtime/nfs \
    -e CCC_NODE_RUN_DIR=/run/ccc-layered \
    -e CCC_MOUNTD_SOCK=/run/ccc-layered/mountd.sock \
    -e CCC_OBSERVE_ROOT=/ccc-runtime/source \
    -e CCC_OBSERVE_MOUNTPOINT=/ccc-runtime/published \
    -e CCC_LOCAL_OVERLAY_ROOT=/ccc-ssd/local-overlays \
    -e CCC_DIRTY_PUBLISH_INTERVAL=0.5 \
    -e CCC_MOUNTD_REQUEST_TIMEOUT=600 \
    -e CCC_MOUNTD_SOCKET_MODE=0600 \
    "$image_tag" >/dev/null
  wait_for_mount "$writer_name" /ccc-runtime/published
}

start_app() {
  "$docker_bin" run -d --rm \
    --name "$app_name" \
    --mount type=bind,src="$docker_run_root/published",dst=/storage/layered,bind-propagation=rslave \
    --mount type=bind,src="$docker_run_root/nfs",dst=/bench/nfs,bind-propagation=rshared \
    --mount type=bind,src="$docker_ssd_run_root/direct",dst=/bench/local,bind-propagation=rshared \
    "$app_image" sh -lc 'while true; do sleep 3600; done' >/dev/null
}

run_target_bench() {
  local workload=$1
  local target=$2
  local root=$3
  local files=$4
  local size_flag=$5
  local size_value=$6
  local seed=$7
  "$docker_bin" exec "$app_name" ccc-layered-benchmark \
    --root "$root" \
    --target "$target" \
    --workload-name "$workload" \
    --files "$files" \
    "$size_flag" "$size_value" \
    --fanout 100 \
    --seed "$seed" \
    --json-out "/bench/nfs/results/${workload}-${target}.json" \
    --no-clean >/dev/null
}

run_workload() {
  local workload=$1
  local files=$2
  local size_flag=$3
  local size_value=$4
  local seed=$5
  local shared_child="shared-${workload}"
  local local_child="local-${workload}"

  echo "benchmark workload=$workload files=$files $size_flag=$size_value"

  mkdir -p "$run_root/nfs/direct/$workload" "$ssd_run_root/direct/$workload"
  run_target_bench "$workload" direct-local "/bench/local/$workload" "$files" "$size_flag" "$size_value" "$seed"
  run_target_bench "$workload" direct-nfs "/bench/nfs/direct/$workload" "$files" "$size_flag" "$size_value" "$seed"

  "$docker_bin" exec "$app_name" sh -lc "mkdir /storage/layered/$shared_child"
  "$docker_bin" exec "$app_name" sh -lc "ls -la /storage/layered/$shared_child >/dev/null 2>&1 || true"
  wait_for_mount "$app_name" "/storage/layered/$shared_child"
  run_target_bench "$workload" layered-shared-nfs "/storage/layered/$shared_child" "$files" "$size_flag" "$size_value" "$seed"
  "$docker_bin" exec "$writer_name" ccc-layered umount "observe:$shared_child" --json >/tmp/ccc-layered-${shared_child}-umount.json
  "$docker_bin" exec "$writer_name" ccc-layered commit "observe:$shared_child" --json >/tmp/ccc-layered-${shared_child}-commit.json

  "$docker_bin" exec "$writer_name" ccc-layered observe-mkdir "$local_child" --json >/tmp/ccc-layered-${local_child}-create.json
  "$docker_bin" exec "$writer_name" ccc-layered write-policy "observe:$local_child" local-ssd-async --json >/tmp/ccc-layered-${local_child}-policy.json
  "$docker_bin" exec "$app_name" sh -lc "ls -la /storage/layered/$local_child >/dev/null"
  wait_for_mount "$app_name" "/storage/layered/$local_child"
  run_target_bench "$workload" layered-local-ssd-async "/storage/layered/$local_child" "$files" "$size_flag" "$size_value" "$seed"
  "$docker_bin" exec "$writer_name" ccc-layered publish "observe:$local_child" --json >/tmp/ccc-layered-${local_child}-publish.json
  "$docker_bin" exec "$writer_name" ccc-layered umount "observe:$local_child" --json >/tmp/ccc-layered-${local_child}-umount.json
  "$docker_bin" exec "$writer_name" ccc-layered commit "observe:$local_child" --json >/tmp/ccc-layered-${local_child}-commit.json
  "$docker_bin" exec "$app_name" sh -lc "ls -la /storage/layered/$local_child >/dev/null 2>&1 || true"
  wait_for_mount "$app_name" "/storage/layered/$local_child"
  "$docker_bin" exec "$app_name" sh -lc "test -s /storage/layered/$local_child/class_000/img_000000.jpg"
}

start_mountd
start_app

run_workload image-small "$small_files" --size-kib "$small_size_kib" 501
run_workload large-files "$large_files" --size-mib "$large_size_mib" 902

"${PYTHON:-python3}" - <<PY
from __future__ import annotations
import json
from pathlib import Path

run_id = "$run_id"
results_dir = Path("$run_root/nfs/results")
workloads = {}
for path in sorted(results_dir.glob("*.json")):
    item = json.loads(path.read_text())
    workload = item["workload"]["name"]
    workloads.setdefault(workload, {})[item["target"]] = item
summary = {
    "run_id": run_id,
    "runtime_root": "$run_root",
    "local_ssd_root": "$ssd_run_root",
    "workloads": [],
    "validation": {
        "min_local_async_speedup_over_shared": float("$min_local_async_speedup"),
        "min_direct_local_speedup_over_nfs": float("$min_direct_local_speedup"),
        "passed": True,
        "failures": [],
    },
}
for workload, targets in sorted(workloads.items()):
    required = {"direct-local", "direct-nfs", "layered-shared-nfs", "layered-local-ssd-async"}
    missing = sorted(required - set(targets))
    if missing:
        summary["validation"]["passed"] = False
        summary["validation"]["failures"].append(f"{workload}: missing {missing}")
        continue
    shared_w = targets["layered-shared-nfs"]["write"]["mib_per_second"]
    local_async_w = targets["layered-local-ssd-async"]["write"]["mib_per_second"]
    direct_nfs_w = targets["direct-nfs"]["write"]["mib_per_second"]
    direct_local_w = targets["direct-local"]["write"]["mib_per_second"]
    async_speedup = local_async_w / shared_w if shared_w else 0.0
    local_speedup = direct_local_w / direct_nfs_w if direct_nfs_w else 0.0
    comparisons = {
        "local_async_vs_shared_write_mibps": async_speedup,
        "direct_local_vs_direct_nfs_write_mibps": local_speedup,
        "local_async_vs_direct_nfs_write_mibps": local_async_w / direct_nfs_w if direct_nfs_w else 0.0,
    }
    if async_speedup < float("$min_local_async_speedup"):
        summary["validation"]["passed"] = False
        summary["validation"]["failures"].append(
            f"{workload}: local-ssd-async/shared speedup {async_speedup:.2f}x below threshold"
        )
    if local_speedup < float("$min_direct_local_speedup"):
        summary["validation"]["passed"] = False
        summary["validation"]["failures"].append(
            f"{workload}: direct-local/direct-nfs speedup {local_speedup:.2f}x below threshold"
        )
    summary["workloads"].append({
        "name": workload,
        "comparisons": comparisons,
        "targets": targets,
    })
out = results_dir / "performance-summary.json"
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, indent=2, sort_keys=True))
if not summary["validation"]["passed"]:
    raise SystemExit("performance validation failed")
PY

printf 'ccc-layered performance benchmark passed\n'
