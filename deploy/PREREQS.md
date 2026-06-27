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

## Real S3/Ceph mirror smoke

For real S3-compatible object-store validation, place credentials in a shell file
that exports only standard AWS variables, then run:

```bash
PYTHON=/home/domen/conda/envs/ccc-dev/bin/python \
CCC_S3_CREDENTIALS_SH=/path/to/s3_storage_premissions.sh \
CCC_S3_ENDPOINT=https://ceph-7.fri.uni-lj.si \
CCC_S3_ADDRESSING_STYLE=auto \
deploy/s3-smoke.sh
```

The script sources the credential file without printing it, creates or validates
a bucket, uploads committed pack and manifest objects, verifies object existence
and byte readback, recalls a cold pack with checksum/size verification, rejects a
corrupt recall without publishing a destination pack, and removes its temporary
objects/bucket unless `CCC_S3_KEEP=1` is set.

For the stronger end-to-end cold-tier and external-HPC exchange validation, run:

```bash
PYTHON=/home/domen/conda/envs/ccc-dev/bin/python \
CCC_S3_CREDENTIALS_SH=/path/to/s3_storage_premissions.sh \
CCC_S3_ENDPOINT=https://ceph-7.fri.uni-lj.si \
CCC_S3_ADDRESSING_STYLE=auto \
deploy/s3-cold-hpc-smoke.sh
```

This smoke creates a real dirty overlay for a dataset child, commits it through
`ccc-layered-mountd` into a SquashFS delta pack, verifies the dirty file inside
the delta, archives the full committed pack stack to S3 cold storage, removes hot
pack files, recalls them from S3 with checksum/size verification, builds and
round-trips an external-HPC packset bundle, and round-trips an HPC output delta
plus provenance through the S3 import-queue metadata path. It still does not SSH
to or submit work on an external HPC.

Ceph RGW compatibility requires boto3/botocore S3v4 with automatic addressing and
request/response checksum calculation set to `when_required`; the
`Boto3ObjectStore` defaults encode this. Some CCC nodes may resolve
`ceph-7.fri.uni-lj.si` only to an unreachable IPv6 address; validate from nodes
that resolve/reach the IPv4 service, such as `morbo`, `calculon`, or
`crushinator` in the current CCC network.

## Privileged no-sidecar Docker runtime smoke

For an actual Docker runtime check where mount authority is fully inside one
privileged Docker container, run:

```bash
CCC_CLIENT_CONTAINERS=domen-cuda10 deploy/privileged-runtime-smoke.sh
```

From a CCC container with the host Docker socket mounted, Docker bind sources are
resolved by the daemon on the host, while the caller may see the same shared
storage through `/storage/user`. In that case pass both roots:

```bash
CCC_RUNTIME_ROOT=/storage/user/ccc-layered-storage-runtime-test \
CCC_RUNTIME_DOCKER_SOURCE_ROOT=/opt/shared_storage/user_data/domen.tabernik@fri.uni-lj.si/ccc-layered-storage-runtime-test \
CCC_CLIENT_CONTAINERS=domen-cuda10 \
deploy/privileged-runtime-smoke.sh
```

This smoke is intentionally privileged and intentionally no-sidecar. It does not
use the CCC FUSE sidecar path; instead it starts `ccc-layered-mountd` inside the
privileged container, mounts a SquashFS child pack, creates a writable
`fuse-overlayfs` view over the shared overlay upper, bind-publishes that view
through a `rshared` Docker bind, asks existing client containers to read/write
the published path, commits the dirty overlay into a delta pack, then unmounts
and remounts the committed child stack to prove the transparent read-only view
contains both original base-pack files and newly committed delta-pack files.

For nested/hierarchical pack validation, run:

```bash
CCC_RUNTIME_ROOT=/storage/user/ccc-layered-storage-nested-test \
CCC_RUNTIME_DOCKER_SOURCE_ROOT=/opt/shared_storage/user_data/domen.tabernik@fri.uni-lj.si/ccc-layered-storage-nested-test \
deploy/nested-runtime-smoke.sh
```

This smoke builds a parent/root SquashFS with `exclude_boundaries=[...]`, verifies
by `unsquashfs` that the parent pack contains only parent files plus a
`.ccc-boundary` mountpoint stub, stores the nested child SquashFS in a separate
`packs/<child-id>/` namespace, then runs `ccc-layered mount-tree` through a real
mountd socket to mount the child directly at the parent boundary path and read
both parent and child data through the combined tree.

By default the script isolates state under:

```text
/storage/user/ccc-layered-storage-runtime-test/runs/<hostname>-<timestamp>-<pid>
```

`CCC_RUNTIME_ROOT` may point under `/storage/user/*`, `/tmp/*`, or this
checkout's `.scratch/*`; broad roots such as `/`, `/storage`, `/storage/user`,
`/storage/datasets`, `/storage/group`, and `/home` are refused.
`CCC_RUNTIME_DOCKER_SOURCE_ROOT` changes only the Docker daemon's bind source
path and must be a specific host-visible subtree, not a broad root such as `/`,
`/storage`, `/storage/user`, `/home`, `/opt`, or `/opt/shared_storage`. Set
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
