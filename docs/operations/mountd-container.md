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
- chowns mountd-created shared storage state to the configured client UID/GID
  (`CCC_STORAGE_USER_ID`/`CCC_STORAGE_GROUP_ID`, or Docker-style
  `USER_ID`/`GROUP_ID` fallback)
- does **not** need the Docker socket

App containers:

- do not get `CCC_MOUNTD_SOCK`
- do not get `/run/ccc-storage`
- do not get `/dev/fuse` for layered storage
- only need `/home` and `/storage` binds with at least `rslave` propagation

## Configuration

Preferred deployments put mountd settings in a TOML file such as
`/etc/ccc-storage/mountd.toml`:

```bash
install -D -m 0640 deploy/config/mountd.example.toml /etc/ccc-storage/mountd.toml
$EDITOR /etc/ccc-storage/mountd.toml
ccc-storage mountd --config /etc/ccc-storage/mountd.toml
```

The file is mountd-owned.  App/client containers should not need S3 credentials,
retention windows, write-policy defaults, or compaction settings; they pass
operations to mountd over the socket.  Environment variables and explicit CLI
flags still work as deployment overrides, with this precedence:

```text
built-in defaults < TOML config < CCC_* environment < explicit mountd flags
```

See `configuration.md` for the section layout and `deploy/config/mountd.example.toml`
for a complete template.

## Build image

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-storage-mountd:local .
```

GitHub Actions uses this same Dockerfile for CI build validation without pushing
an image. The manual dev Docker workflow logs in to Docker Hub and publishes
`vicoslab/ccc-storage:dev`. On a release tag matching `v<digits>.<digits>` (for
example `v1.0` or `v0.01`), the release workflow publishes
`vicoslab/ccc-storage:<tag>` plus updates `vicoslab/ccc-storage:latest`. This
image is the mountd service image only; client/user CLI tools are installed
separately into client containers with `pip`.

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
- `--compaction-interval 3600`: optional background log-structured compaction scan.
- `--cold-storage-interval 604800`: optional automatic cold-storage archival scan;
  `<=0` disables the scan. Prefer `[cold_storage] interval_seconds` in the
  mountd TOML config for deployments; `CCC_COLD_STORAGE_*`/`CCC_S3_*`
  environment variables remain supported as overrides.
- `--storage-uid` / `--storage-gid`: owner forced for mountd-created shared
  state and committed SquashFS metadata. The container entrypoint reads
  `CCC_STORAGE_USER_ID`/`CCC_STORAGE_GROUP_ID` first, then `USER_ID`/`GROUP_ID`.
  CCC development validation defaults these to `2094:2094`.

## Inventory role

The CCC inventory role is `ccc-storage-mountd`.  It is default-off.  See the inventory repo:

```text
ccc-inventory/playbook/roles/ccc-storage-mountd/README.md
```
