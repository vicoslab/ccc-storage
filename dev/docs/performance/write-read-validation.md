# CCC layered write/read performance validation

`dev/validation/performance/performance-runtime-benchmark.sh` is the full write/read performance
validation for the layered-storage stack.  It complements the smaller functional
runtime smokes by comparing the actual storage paths users care about:

1. direct node-local SSD bind;
2. direct NFS bind;
3. CCC layered child using `shared-nfs` (`fuse-overlayfs` with NFS upper/work);
4. CCC layered child using `local-ssd-async` (kernel OverlayFS with local SSD
   upper/work and commit back through NFS mirror/pack state).

The benchmark writes deterministic JPEG-like mostly-incompressible payloads,
runs `sync` after writes, reads full file bytes back into SHA-256 checksums, and
stores JSON results under the run's NFS result directory.

## Default validation workloads

The defaults intentionally cover both required operating regimes:

| Workload | Shape | Purpose |
|---|---:|---|
| `image-small` | `2,000 × 512 KiB` | Thousands of image-like files in the 500 KiB class. |
| `large-files` | `4 × 128 MiB` | Fewer large files, each greater than 100 MiB. |

These can be adjusted with:

```bash
CCC_PERF_SMALL_FILES=2000
CCC_PERF_SMALL_SIZE_KIB=512
CCC_PERF_LARGE_FILES=4
CCC_PERF_LARGE_SIZE_MIB=128
```

## CCC/Docker-host run command

When running from a CCC container that talks to the host Docker socket, pass both
container-visible roots and Docker-host source roots:

```bash
CCC_RUNTIME_KEEP=1 \
CCC_RUNTIME_ROOT=/storage/user/ccc-layered-storage-performance-test \
CCC_RUNTIME_DOCKER_SOURCE_ROOT=/opt/shared_storage/user_data/<ccc-user-id>/ccc-layered-storage-performance-test \
CCC_LOCAL_SSD_ROOT=/tmp/ccc-layered-storage-performance-local \
CCC_LOCAL_SSD_DOCKER_SOURCE_ROOT=/tmp/ccc-layered-storage-performance-local \
CCC_PERF_TIMEOUT=600 \
dev/validation/performance/performance-runtime-benchmark.sh
```

The script builds `ccc-layered-storage-mountd:local` unless
`CCC_RUNTIME_SKIP_BUILD=1` is set.

## Validation gates

The benchmark fails if:

- any target/workload result is missing;
- the layered `local-ssd-async` write throughput is slower than the layered
  `shared-nfs` write throughput for a workload, unless overridden with
  `CCC_PERF_MIN_LOCAL_ASYNC_SPEEDUP`;
- an optional direct-local/direct-NFS threshold set through
  `CCC_PERF_MIN_DIRECT_LOCAL_SPEEDUP` is not met.

The direct-local/direct-NFS comparison is reported by default but not used as a
strict portability gate because local scratch device and NFS cache behavior vary
by node.  Set `CCC_PERF_MIN_DIRECT_LOCAL_SPEEDUP` for site-specific acceptance.

## Validated `donbot` result

Final validation run:

- Run id: `donbot-20260630T141650Z-42250`
- Artifact: `dev/docs/benchmarks/performance-summary-donbot-20260630T141650Z.json`
- Runtime root: `/storage/user/ccc-layered-storage-performance-test/runs/donbot-20260630T141650Z-42250`
- Validation: passed

### Write throughput

| Workload | Direct local SSD | Direct NFS | Layered `shared-nfs` | Layered `local-ssd-async` | local-async/shared |
|---|---:|---:|---:|---:|---:|
| `image-small` (`2,000 × 512 KiB`) | `176.98 MiB/s` | `101.50 MiB/s` | `43.72 MiB/s` | `172.65 MiB/s` | `3.95×` |
| `large-files` (`4 × 128 MiB`) | `148.55 MiB/s` | `89.38 MiB/s` | `82.61 MiB/s` | `117.03 MiB/s` | `1.42×` |

### Read throughput

| Workload | Direct local SSD | Direct NFS | Layered `shared-nfs` | Layered `local-ssd-async` |
|---|---:|---:|---:|---:|
| `image-small` | `405.56 MiB/s` | `293.54 MiB/s` | `251.63 MiB/s` | `385.46 MiB/s` |
| `large-files` | `384.75 MiB/s` | `327.87 MiB/s` | `380.70 MiB/s` | `377.94 MiB/s` |

Takeaways from this run:

- For the requested thousands-of-image-files workload, `local-ssd-async` write
  throughput was close to direct local SSD and nearly `4×` faster than layered
  `shared-nfs`.
- For large sequential files, `local-ssd-async` still beat layered `shared-nfs`,
  but by a smaller margin because per-file metadata/open/close overhead is less
  dominant.
- Full-byte read checksums matched across all targets for each workload.

## Result artifacts

A successful run writes:

```text
<runtime-root>/runs/<run-id>/nfs/results/performance-summary.json
<runtime-root>/runs/<run-id>/nfs/results/<workload>-<target>.json
```

Copy the final `performance-summary.json` into `dev/docs/benchmarks/` for release or
review records.
