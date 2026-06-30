# CCC layered storage

CCC layered storage reduces shared-NFS pressure from millions of small files by
serving normal-looking folders from immutable SquashFS packs plus a small shared
write overlay.

The first production target is CCC conda envs and dataset folders:

```text
many small files on NFS
  -> commit into one SquashFS pack
  -> users still see a normal directory
  -> new writes go to a shared overlay
  -> commit creates the next SquashFS generation
```

## Concepts

- **Pack**: a SquashFS file containing a committed generation.
- **Overlay**: shared writable upperdir for changes before the next commit.
- **Marker**: `CCC_LAYERED_OBSERVE` in a parent folder tells mountd to observe
  immediate child folders as layered children.
- **mountd**: the dedicated Docker container that owns FUSE and publishes the
  live folder view. Normal CCC app containers do not get the mountd socket.
- **App container bind propagation**: app containers only need `/home` and
  `/storage` binds with `rslave` propagation to see host-created layered mounts.

## Build

Development/test image:

```bash
docker build -f dev/docker/test.Dockerfile -t ccc-layered-storage:test .
```

Production mountd image:

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-layered-storage-mountd:local .
```

## Run checks

```bash
make lint
make test
```

Development runtime smoke for the mountd/app-container topology:

```bash
CCC_MOUNTD_IMAGE=ccc-layered-storage-mountd:local \
dev/validation/docker/mountd-container-runtime-smoke.sh
```

This starts a privileged mountd container and a separate unprivileged app
container, writes through the app-visible folder, commits, remounts, and verifies
that the app never receives mountd env/socket/FUSE privileges. Development-only
validation scripts live under `dev/`; deployment files live under `deploy/`.

## Use from CCC

Mark a conda envs folder for layered observation:

```bash
ccc-storage init-conda-envs /storage/user/layered-source/conda/envs
```

Basic management commands:

```bash
ccc-storage doctor
ccc-storage observe-ls
ccc-storage observe-mkdir my-env
ccc-storage status observe:my-env
ccc-storage commit observe:my-env -m "updated env"
```

Safe conda/mamba wrappers:

```bash
ccc-storage conda install -n my-env numpy
ccc-storage mamba update -n my-env --all
```

The wrappers pass through to normal conda/mamba unless the target is an explicit
managed layered env. If mountd/shared state is absent, normal conda still works.

## CCC inventory deployment

Use the default-off inventory role:

```text
../ccc-inventory-layered-storage/playbook/roles/layered-storage-mountd/
```

Normal compute containers should not receive mountd socket/env.  If they need to
see layered mounts created on the host, opt them into bind propagation only:

```yaml
ENABLE_STORAGE_MOUNT_PROPAGATION: true
```

## More details

- Deployment artifacts: `deploy/README.md`
- Development validation and benchmarks: `dev/README.md`
- Mountd container operation: `docs/operations/mountd-container.md`
- CLI tools reference: `docs/operations/cli-tools.md`
- Conda/mamba shim: `docs/operations/conda-shim.md`
- Log-structured compaction operation: `docs/operations/log-structured-compaction.md`
- System design for log-structured pack levels and partial compaction: `../docs/components/log-structured-pack-levels.md`
- Cross-system implementation plan for pack levels and tombstones: `../docs/implementation/log-structured-pack-levels-implementation-plan.md`
- Conda-style metadata benchmark: `dev/docs/performance/conda-small-files-smoke.md`
