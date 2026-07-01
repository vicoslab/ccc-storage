# CCC storage

CCC storage reduces shared-NFS pressure from millions of small files by
serving normal-looking folders from immutable SquashFS packs plus a small shared
write overlay.

```text
many small files on NFS
  -> commit into one SquashFS pack
  -> users still see a normal directory
  -> new writes go to a shared overlay
  -> commit creates the next SquashFS generation
```

## Concepts

- **Pack**: a SquashFS files containing a committed generation in LSM-tree structure for optimal merging.
- **Overlay**: shared writable upperdir for changes before the next commit with support for faster but local (SSD) and shared but slower mounts (NFS).
- **Container-ready**: the dedicated Docker container that owns FUSE and publishes the live folder view, client containers do not get the mountd socket.
- **Conda-ready**: support for use in conda/mamba as storage for environments.
- **Cold storage:** generic archive/recall support for committed SquashFS packs.
  S3-compatible object storage is the current backend; archive mode can evict
  hot packs, while mirror mode keeps hot packs and maintains a backend copy.
- **HPC integration:** support for accessing the data from HPC systems (e.g. SLURM) through FUSE with pre-loading and on-demand (lazy) reading.

## Build

Development/test image:

```bash
docker build -f dev/docker/test.Dockerfile -t ccc-storage:test .
```

Production mountd image:

```bash
docker build -f deploy/docker/mountd.Dockerfile -t ccc-storage-mountd:local .
```

CI runs tests but does not build or push Docker images. The manual dev Docker
workflow builds `deploy/docker/mountd.Dockerfile` and publishes the mountd image
to Docker Hub as `vicoslab/ccc-storage:dev`. Pushing a release tag matching
`v<digits>.<digits>` (for example `v1.0` or `v0.01`) publishes
`vicoslab/ccc-storage:<tag>` and updates `vicoslab/ccc-storage:latest`. The
published Docker image is intentionally the mountd service image only;
user-facing CLI tools are installed into client containers with `pip`.

## Run checks

```bash
make lint
make test
```

Development runtime smoke for the mountd/app-container topology:

```bash
CCC_MOUNTD_IMAGE=ccc-storage-mountd:local \
dev/validation/docker/mountd-container-runtime-smoke.sh
```

This starts a privileged mountd container and a separate unprivileged app
container, writes through the app-visible folder, commits, remounts, and verifies
that the app never receives mountd env/socket/FUSE privileges. Development-only
validation scripts live under `dev/`; deployment files live under `deploy/`.

## Use from client container

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
ccc-storage cold status observe:my-env --json
ccc-storage commit observe:my-env -m "updated env"
```

Safe conda/mamba wrappers:

```bash
ccc-storage conda install -n my-env numpy
ccc-storage mamba update -n my-env --all
```

The wrappers pass through to normal conda/mamba unless the target is an explicit
managed layered env. If mountd/shared state is absent, normal conda still works.

## More details

- Deployment artifacts: `deploy/README.md`
- Mountd configuration: `docs/operations/configuration.md`
- CLI tools reference: `docs/operations/cli-tools.md`
- Cold storage operation and design: `docs/operations/cold-storage.md`
- Mountd container operation: `docs/operations/mountd-container.md`
- Conda/mamba shim: `docs/operations/conda-shim.md`
- Log-structured compaction operation: `docs/operations/log-structured-compaction.md`
- System design for log-structured pack levels and partial compaction: `../docs/components/log-structured-pack-levels.md`
- Cross-system implementation plan for pack levels and tombstones: `../docs/implementation/log-structured-pack-levels-implementation-plan.md`
- Development validation and benchmarks: `dev/README.md`
- Conda-style metadata benchmark: `dev/docs/performance/conda-small-files-smoke.md`

