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
docker build -t ccc-layered-storage:test .
```

Production mountd image:

```bash
docker build -f deploy/Dockerfile.mountd -t ccc-layered-mountd:local .
```

## Run checks

```bash
make lint
make test
```

Runtime smoke for the production topology:

```bash
CCC_MOUNTD_IMAGE=ccc-layered-mountd:local \
deploy/mountd-container-runtime-smoke.sh
```

This starts a privileged mountd container and a separate unprivileged app
container, writes through the app-visible folder, commits, remounts, and verifies
that the app never receives mountd env/socket/FUSE privileges.

## Use from CCC

Mark a conda envs folder for layered observation:

```bash
ccc-layered init-conda-envs /storage/user/layered-source/conda/envs
```

Basic management commands:

```bash
ccc-layered doctor
ccc-layered observe-ls
ccc-layered observe-mkdir my-env
ccc-layered status observe:my-env
ccc-layered commit observe:my-env -m "updated env"
```

Safe conda/mamba wrappers:

```bash
ccc-conda install -n my-env numpy
ccc-mamba update -n my-env --all
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

- Mountd container operation: `docs/operations/mountd-container.md`
- Conda/mamba shim: `docs/operations/conda-shim.md`
- Conda-style metadata benchmark: `docs/performance/conda-small-files-smoke.md`
- Image-like small-file benchmark: `docs/performance/image-small-files-5000x32k.md`
- Runtime prerequisites: `deploy/PREREQS.md`
