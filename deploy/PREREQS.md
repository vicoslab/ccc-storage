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

For a scratch-only control-plane smoke test that does not touch production
`/storage/.ccc-layered`, run from the repository checkout:

```bash
deploy/runtime-smoke.sh
```

The script creates a temporary root under `/tmp` by default, starts a local
`ccc-layered-mountd`, exercises `ccc-layered doctor`, `create`, and `parent-ls`,
then removes the scratch tree.

For real SquashFS build/verify/extract plus an unprivileged FUSE mount check,
run:

```bash
deploy/fuse-smoke.sh
```

This uses a git-ignored repo-local `.scratch/` root by default so CCC's
`fusermount3` sidecar shim sees the mountpoint under the container's shared
workspace bind. `CCC_SMOKE_ROOT` may point under `/tmp` or inside the repository
checkout, but the script refuses other roots. On plain hosts with a normal
`fusermount3`, `/tmp` is fine; in CCC containers prefer the default repo-local
scratch path. If unprivileged FUSE is unavailable and you still want
build/verify/extract coverage, set `CCC_ALLOW_FUSE_SKIP=1`; the mount step will
print `skip with reason` and exit successfully.

For a local Docker validation image that does not push anything and uses only
scratch container storage, run:

```bash
deploy/docker-smoke.sh
```

It builds the repository `Dockerfile` with a local tag, then runs unit tests,
`deploy/runtime-smoke.sh`, and `deploy/fuse-smoke.sh` inside one smoke
container. `/dev/fuse`, `CAP_SYS_ADMIN`, and the AppArmor mount relaxation are
passed only to that container.

## Privileged no-sidecar Docker runtime smoke

For an actual Docker runtime check where mount authority is fully inside one
privileged Docker container, run:

```bash
CCC_CLIENT_CONTAINERS=domen-cuda10 deploy/privileged-runtime-smoke.sh
```

This smoke is intentionally privileged and intentionally no-sidecar. It does not
use the CCC FUSE sidecar path; instead it starts `ccc-layered-mountd` inside the
privileged container, mounts a SquashFS child pack, creates a writable
`fuse-overlayfs` view over the shared overlay upper, bind-publishes that view
through a `rshared` Docker bind, asks existing client containers to read/write
the published path, and commits the dirty overlay into a delta pack.

By default the script isolates state under:

```text
/storage/user/ccc-layered-storage-runtime-test/runs/<hostname>-<timestamp>-<pid>
```

`CCC_RUNTIME_ROOT` may point under `/storage/user/*`, `/tmp/*`, or this
checkout's `.scratch/*`; broad roots such as `/`, `/storage`, `/storage/user`,
`/storage/datasets`, `/storage/group`, and `/home` are refused. Set
`CCC_RUNTIME_KEEP=1` to retain the per-run directory for inspection.

For a manual multi-node pass, run the same command from each target node, for
example:

```bash
for node in donbot morbo calculon crushinator flexo kif zapp; do
  ssh "$node" 'cd /path/to/ccc-layered-storage && CCC_CLIENT_CONTAINERS=domen-cuda10 deploy/privileged-runtime-smoke.sh'
done
```

This is a manual privileged runtime smoke; it is not part of the default
non-privileged validation lane.

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
