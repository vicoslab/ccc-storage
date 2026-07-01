# Development artifacts

This directory contains repository-local development helpers: validation scripts,
benchmarks, CCC-node smoke tests, and recorded benchmark outputs. These files are
useful for developing and validating the project, but they are not required for a
normal server deployment.

## Layout

```text
dev/
  docker/
    test.Dockerfile             # optional development/test image
  bench/                       # standalone benchmark workload generators
  validation/
    local/                     # local package/FUSE/Docker smoke checks
    docker/                    # real Docker/FUSE runtime topology smokes
    s3/                        # real S3/Ceph and cold/HPC exchange smokes
    performance/               # full write/read performance validation
  docs/
    operations/                # development-node prerequisites and caveats
    performance/               # benchmark descriptions and checked-in summaries
    benchmarks/                # raw JSON benchmark artifacts from development runs
```

## Development/test image

```bash
docker build -f dev/docker/test.Dockerfile -t ccc-storage:test .
```

This image installs development/test extras and defaults to `make test`; it is
not the mountd deployment image.

## Common validation entrypoints

```bash
# Scratch-only local control-plane smoke.
dev/validation/local/runtime-smoke.sh

# SquashFS build/verify/extract plus optional unprivileged FUSE mount.
dev/validation/local/fuse-smoke.sh

# Dedicated mountd container + unprivileged app topology smoke.
dev/validation/docker/mountd-container-runtime-smoke.sh

# Per-child write-policy Docker/FUSE smoke.
dev/validation/docker/write-policy-runtime-smoke.sh

# Full write/read benchmark across local SSD, direct NFS, shared-nfs, and local-ssd-async.
dev/validation/performance/performance-runtime-benchmark.sh
```

CCC development-node prerequisites and caveats live in
`dev/docs/operations/node-prerequisites.md`.
