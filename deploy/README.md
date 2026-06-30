# Deployment and validation artifacts

This directory is intentionally organized by operational role so release readers
can tell production deployment files from development/runtime validation helpers.

## Layout

```text
deploy/
  docker/
    mountd.Dockerfile          # dedicated ccc-layered-mountd service image
    mountd-entrypoint.sh       # entrypoint used by the mountd image
  systemd/
    ccc-layered-mountd.service # opt-in host-level systemd unit template
    install.sh                 # copy/reload helper; does not enable/start
    uninstall.sh               # remove/reload helper
  validation/
    local/                     # local package/FUSE/Docker smoke checks
    docker/                    # real Docker/FUSE runtime topology smokes
    s3/                        # real S3/Ceph and cold/HPC exchange smokes
    performance/               # full write/read performance validation
```

## Dockerfiles

The repository has two Dockerfiles with different roles:

- `Dockerfile` at the repository root is the optional development/test image. It
  installs dev/test extras and defaults to `make test`.
- `deploy/docker/mountd.Dockerfile` is the dedicated runtime image for the
  `ccc-layered-mountd` service. It installs only the manifest/FUSE runtime
  extras, uses `deploy/docker/mountd-entrypoint.sh`, and includes a mountd
  health check.

Keep new Docker image definitions under `deploy/docker/` unless they are the
repository-wide default development image.

## Important validation entrypoints

```bash
# Scratch-only local control-plane smoke.
deploy/validation/local/runtime-smoke.sh

# SquashFS build/verify/extract plus optional unprivileged FUSE mount.
deploy/validation/local/fuse-smoke.sh

# Dedicated mountd container + unprivileged app topology smoke.
deploy/validation/docker/mountd-container-runtime-smoke.sh

# Per-child write-policy Docker/FUSE smoke.
deploy/validation/docker/write-policy-runtime-smoke.sh

# Full write/read benchmark across local SSD, direct NFS, shared-nfs, and local-ssd-async.
deploy/validation/performance/performance-runtime-benchmark.sh
```

Node prerequisites and longer operational notes live in
`docs/operations/node-prerequisites.md`.
