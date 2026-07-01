# Deployment artifacts

This directory contains operator-facing files for deploying `ccc-storage`
on a server. Development-only validation, benchmark, and environment-specific
smoke scripts live under `dev/`.

## Layout

```text
deploy/
  config/
    mountd.example.toml        # operator-facing mountd TOML configuration example
  docker/
    mountd.Dockerfile          # dedicated ccc-storage mountd service image
    mountd-entrypoint.sh       # entrypoint used by the mountd image
  systemd/
    ccc-storage-mountd.service # opt-in host-level systemd unit template
    install.sh                 # copy/reload helper; does not enable/start
    uninstall.sh               # remove/reload helper
```

## Configuration

Use `deploy/config/mountd.example.toml` as the template for
`/etc/ccc-storage/mountd.toml`.  Mountd owns this configuration; unprivileged
client containers should normally interact through the socket and should not need
S3, retention, compaction, or mount policy settings.

## Docker image

Build the dedicated runtime image from the repository root:

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-storage-mountd:local .
```

`mountd.Dockerfile` installs the package with its manifest/FUSE runtime extras,
uses `deploy/docker/mountd-entrypoint.sh`, and exposes a mountd health check. It
is separate from `dev/docker/test.Dockerfile`, which is a development/test
image.

CI runs tests but does not build or push Docker images. The manual dev Docker
workflow builds this Dockerfile and pushes `vicoslab/ccc-storage:dev`. Tags
matching `v<digits>.<digits>` (for example `v1.0` or `v0.01`) are pushed to
Docker Hub as `vicoslab/ccc-storage:<tag>` and update
`vicoslab/ccc-storage:latest`. These workflows publish the mountd image, not a
client CLI image. Client containers should install the user-facing `ccc-storage`
CLI with `pip` when that integration is added.

## Systemd unit

`deploy/systemd/ccc-storage-mountd.service` is an opt-in template for running the
mount daemon as a host service. The install helper copies the unit and runs
`systemctl daemon-reload`; it deliberately does not enable or start the service.

```bash
sudo deploy/systemd/install.sh
# edit environment/paths for the target host, then enable/start explicitly
sudo systemctl enable --now ccc-storage-mountd.service
```

Use `deploy/systemd/uninstall.sh` to remove the unit and reload systemd.

## Development validation

Runtime smokes, S3/Ceph checks, performance benchmarks, and recorded development
results are intentionally not mixed into `deploy/`. See `dev/README.md`.
