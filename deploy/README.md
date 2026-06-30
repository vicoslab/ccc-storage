# Deployment artifacts

This directory contains operator-facing files for deploying `ccc-layered-storage`
on a server. Development-only validation, benchmark, and environment-specific
smoke scripts live under `dev/`.

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
```

## Docker image

Build the dedicated runtime image from the repository root:

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-layered-mountd:local .
```

`mountd.Dockerfile` installs the package with its manifest/FUSE runtime extras,
uses `deploy/docker/mountd-entrypoint.sh`, and exposes a mountd health check. It
is separate from the repository-root `Dockerfile`, which is a development/test
image.

## Systemd unit

`deploy/systemd/ccc-layered-mountd.service` is an opt-in template for running the
mount daemon as a host service. The install helper copies the unit and runs
`systemctl daemon-reload`; it deliberately does not enable or start the service.

```bash
sudo deploy/systemd/install.sh
# edit environment/paths for the target host, then enable/start explicitly
sudo systemctl enable --now ccc-layered-mountd.service
```

Use `deploy/systemd/uninstall.sh` to remove the unit and reload systemd.

## Development validation

Runtime smokes, S3/Ceph checks, performance benchmarks, and recorded development
results are intentionally not mixed into `deploy/`. See `dev/README.md`.
