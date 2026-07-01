# Mountd container operation

`ccc-storage mountd` should run as a dedicated Docker container.  Normal CCC app
containers do not need to know about mountd.

## Required container properties

Mountd container:

- has `/dev/fuse`
- has `SYS_ADMIN` or equivalent FUSE/mount capability
- uses unconfined AppArmor/seccomp when required by the host FUSE setup
- mounts the shared storage root with `rw,rshared`
- owns the private control socket under `/run/ccc-storage`
- does **not** need the Docker socket

App containers:

- do not get `CCC_MOUNTD_SOCK`
- do not get `/run/ccc-storage`
- do not get `/dev/fuse` for layered storage
- only need `/home` and `/storage` binds with at least `rslave` propagation

## Build image

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-storage-mountd:local .
```

## Development smoke topology

A repository checkout includes a development-only Docker/FUSE smoke for this
mountd/app-container topology:

```bash
CCC_MOUNTD_IMAGE=ccc-storage-mountd:local \
dev/validation/docker/mountd-container-runtime-smoke.sh
```

The smoke starts two containers: privileged mountd and unprivileged app. It
checks that the app has no mountd socket/env/FUSE access, writes through the
published layered folder, commits, remounts, and reads the committed data. See
`dev/README.md` for validation details.

## Important mountd flags

- `--socket-mode 0600`: private control socket by default.
- `--ready-file /run/ccc-storage/ready.json`: doctor JSON written after socket readiness.
- `--observe-ready-timeout 10`: fail startup if the observation FUSE mount is not ready.
- `--idle-unmount-ttl 300`: clean up idle child mounts.
- `--idle-reap-interval 30`: cleanup interval.

## Inventory role

The CCC inventory role is `layered-storage-mountd`.  It is default-off.  See:

```text
../ccc-inventory-layered-storage/playbook/roles/layered-storage-mountd/README.md
```
