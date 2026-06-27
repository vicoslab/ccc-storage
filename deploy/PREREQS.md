# CCC layered storage node prerequisites

`ccc-layered-mountd` is intended to run as a **host-level, opt-in daemon** on CCC
nodes. It is a local control plane for SquashFS/FUSE-backed managed paths; NFS
remains the hot shared truth for manifests, overlays, and hot pack files.

## Required shared storage

- NFS-mounted shared state directory, default:

  ```text
  /storage/.ccc-layered
  ```

- Managed parents should be introduced as **new mountpoints first**, not by
  replacing production paths in-place. Validate a new parent, then migrate users.

## Required local capabilities

Minimum read-only/runtime stack:

- Linux kernel with SquashFS support, or `squashfuse` fallback.
- `/dev/fuse` available when using user-space FUSE adapters.
- `fusermount3` available for unprivileged FUSE paths.
- `fuse-overlayfs` or kernel OverlayFS for writable union/runtime lanes.
- Python 3.11+ and the `ccc-layered-storage` package entry points on PATH.

Privileged host daemon mode:

- `CAP_SYS_ADMIN` for local mount/unmount operations.
- `/dev/fuse` read/write access.
- Mount propagation configured as `rslave` where containers must observe child
  mounts created after container start.

## Degraded modes

- If `/dev/fuse` or `fusermount3` is unavailable, read-only control-plane and
  unit tests still work; real FUSE mount tests skip with a clear reason.
- If Docker is unavailable, Docker propagation tests skip with a clear reason.
- If real S3 credentials are absent, real-S3 tests skip; local object-store tests
  still validate mirror/recall semantics.

## Preflight

Run on a candidate node:

```bash
ccc-layered-mountd --probe
python -c "from tests.fakes.capability import CAPS; print(CAPS)"
```

Then start against a non-production managed parent first:

```bash
sudo systemctl start ccc-layered-mountd
sudo systemctl status ccc-layered-mountd
ccc-layered doctor
```

## Rollback

Stop the daemon and remove the unit:

```bash
sudo systemctl stop ccc-layered-mountd
sudo deploy/uninstall.sh
```

Committed packs and manifests are immutable/backward-compatible within a schema
major; rollback of the daemon does not delete shared pack data.
