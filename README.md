# ccc-layered-storage

SquashFS-backed **layered storage** for the CCC compute cluster — a monorepo of
five independently-buildable Python packages plus a shared test harness.

> **Current status — Phase 03 dirty overlay + manual commit foundation.** Phase
> 00 created the safe dev/test harness. Phase 01 added immutable pack metadata,
> TOML manifests, locks, checksums, and `ccc-pack`. Phase 02 added the
> newline-JSON control protocol, node-local `MountdService`, Unix control
> socket, and child-mount refcounting abstraction. Phase 03 adds shared-overlay
> directory bookkeeping, dirty stats, active→sealed overlay rotation, manual
> commit locking, delta-pack publication, and `ccc-layered commit`. Managed
> parent FUSE, auto-commit, S3 mirroring, and external-HPC flows remain later
> phases.

---

## ⚠️ Safety & isolation (read first)

Tests in this repo **must never touch real `/storage` datasets, users, or the
real `/storage/.ccc-layered`**, and must never require Docker or root.

- All runtime test artifacts (packs, manifests, overlays, sockets, fake-NFS,
  fake-S3) live under **`$CCC_TEST_ROOT`**, which defaults to the git-ignored
  **`.scratch/`** directory inside this repo.
- A hard isolation guard (`tests/conftest.py` → `tests/fakes/isolation.py`)
  asserts at session start that `$CCC_TEST_ROOT` resolves **inside the
  workspace**. If it points anywhere else (e.g. `/tmp`, `/storage/dataset`) the
  **entire test session aborts** before any filesystem access (risk RK-13).
- Everything runs **unprivileged and Docker-free**. Privilege, when needed by
  later phases, comes from user+mount namespaces (`unshare -rm`), not root.
  FUSE / kernel-mount / multinode / Docker tiers are **capability-gated**: the
  active probe in `tests/fakes/capability.py` reports what this host can do and
  dependent tests **skip with a reason** rather than failing or silently
  passing.

Do **not** point `$CCC_TEST_ROOT` at a real dataset to "test against real data".
There are fakes for all external dependencies.

---

## Layout

```
src/
  ccc_layered_core/    shared contracts (manifest schema, locks, protocol) — no FUSE/net
  ccc_layered_pack/    SquashFS build/verify/read library + `ccc-pack` CLI
  ccc_layered_mountd/  per-node privileged daemon + `ccc-layered-mountd`
  ccc_layered_cli/     unprivileged user/container CLI `ccc-layered`
  ccc_layered_hpc/     S3 mirror / external-HPC export-import + `ccc-layered-hpc`
tests/
  conftest.py          isolation guard + marker registration + fixtures
  fakes/               capability probe, fake-NFS, fake-S3, node harness, gen_trees
  unit/                fast, no-privilege tier (sub-second target)
  fuse/ multinode/ bench/   capability-gated tiers (mostly empty until later phases)
```

Entry points:

| Command | Package | Status |
|---|---|---|
| `ccc-pack` | `ccc_layered_pack.cli:main` | implemented: `build`, `verify`, `manifest show` |
| `ccc-layered-mountd` | `ccc_layered_mountd.daemon:main` | implemented: control socket + manifest/status/mount service |
| `ccc-layered` | `ccc_layered_cli.main:main` | implemented: `doctor`, `status`, `ls`, `mount`, `umount`, `commit` |
| `ccc-layered-hpc` | `ccc_layered_hpc.client:main` | stub (phase-08) |

---

## Developer setup

### Option A — conda env (canonical, pinned)

```bash
make env                # mamba/conda env create -f environment.yml
conda activate ccc-dev  # documented alias: ccc-layered-dev
```

The `ccc-dev` env carries the full toolchain (squashfs-tools, squashfuse,
fuse-overlayfs, pyfuse3, pytest+plugins, ruff, mypy, moto[s3], …). See
`environment.yml`.

### Option B — pip (CI / lightweight fallback)

```bash
python -m pip install -e '.[dev]'
```

This installs the lint/test toolchain and the importable packages. Note that
`moto` is **not** in the pip `dev` extra — fake-S3 tests skip gracefully when it
is absent (use the conda env, or `pip install 'moto[s3]'`, to exercise them).

---

## Everyday commands

```bash
make lint          # ruff check + mypy (src)
make fmt           # ruff format + ruff --fix
make test          # unit tier only — fast, no FUSE, no privilege
make test-fuse     # unit + unprivileged FUSE tier (capability-gated)
make test-multinode# unit + multinode tier
make test-all      # everything the capability probe allows (no Docker)
make bench         # performance smoke benchmarks
make probe         # print the capability probe result as JSON
make clean         # remove caches and the .scratch test root
```

Run the unit tier directly:

```bash
python -m pytest tests/unit -q
```

Inspect what this host can do:

```bash
python -c "from tests.fakes.capability import CAPS; print(CAPS)"
```

### Environment variables

| Var | Meaning | Default |
|---|---|---|
| `CCC_TEST_ROOT` | Root for all test artifacts; **must** be inside the workspace | `<repo>/.scratch` |
| `CCC_NFS_ROOT` | Path to the (fake) `.ccc-layered` shared state | set per-test by the `fake_nfs` fixture |
| `CCC_PROBE_TIMEOUT` | Per-probe timeout in seconds for the capability probe | `5` |
| `CCC_MOUNTD_SOCK` | mountd control-socket path (used by `ccc-layered doctor`) | `/run/ccc-layered/mountd.sock` |

---

## Test tiers (see `implementation-planning/testing/test-strategy.md`)

| Tier | Dir | Needs | Marker |
|---|---|---|---|
| Unit | `tests/unit/` | nothing | — |
| FUSE (unpriv) | `tests/fuse/` | `/dev/fuse`, squashfuse, fuse-overlayfs | `@fuse` |
| FUSE (kernel) | `tests/fuse/` | mount priv via `unshare -rm` | `@kernel_mount`, `@userns` |
| Multinode | `tests/multinode/` | fake-NFS + node harness | `@multinode` |
| Bench | `tests/bench/` | fixtures | `@bench` |
| Docker | `tests/docker/` | Docker daemon (optional lane) | `@docker` |

The unit tier targets **sub-second** total runtime. Capability-gated tiers skip
(never silently pass) when the probe reports the capability unavailable.

---

## Phase status

**Phase 00 complete:** conda env spec, monorepo skeleton (`pyproject`,
`Makefile`, package dirs + entry points), capability probe, fake-NFS / fake-S3 /
node harness, synthetic-tree generators, conftest isolation guard + marker
wiring, unit tests for all of the above, and a safe CI skeleton.

**Phase 01 complete:**

- `ccc_layered_core.manifest`: atomic TOML child manifests with pack stack,
  overlay/S3 fields, and hierarchical child-boundary metadata.
- `ccc_layered_core.locks`: NFS-safe `O_CREAT|O_EXCL` lockfiles with holder
  metadata, heartbeat, and stale-lock stealing.
- `ccc_layered_core.checksum`: streaming SHA-256 helpers.
- `ccc_layered_core.resolve`: nearest child-boundary resolution.
- `ccc_layered_pack.builder`: `mksquashfs` wrapper with deterministic defaults
  and child-boundary exclusion.
- `ccc_layered_pack.verify`: checksum/size verification.
- `ccc_layered_pack.reader`: simple `squashfuse` mount and `unsquashfs` extract
  helpers for later FUSE tests.
- `ccc_layered_pack.bundle`: tar packset bundle seed for later S3/HPC transfer.
- `ccc-pack build`, `ccc-pack verify`, and `ccc-pack manifest show`.

**Phase 02 complete:**

- `ccc_layered_core.protocol`: newline-delimited JSON request/response protocol.
- `ccc_layered_mountd.control`: Unix-domain control socket server.
- `ccc_layered_mountd.childmount`: read-only child mount lifecycle/refcounting
  abstraction. It delegates bytes to `squashfuse`/future kernel mounts and keeps
  mountd out of the read path.
- `ccc_layered_mountd.daemon.MountdService`: scans NFS registry manifests,
  reports status/ls, and handles explicit mount/umount/doctor requests.
- `ccc-layered status`, `ccc-layered ls`, `ccc-layered mount`, and
  `ccc-layered umount` over the mountd socket.

**Phase 03 complete:**

- `ccc_layered_mountd.overlay`: shared-NFS overlay path layout, active upper
  creation, dirty file/byte accounting, and active→sealed rotation.
- `ccc_layered_pack.builder.build_delta`: simple delta-pack builder for sealed
  dirty uppers.
- `ccc_layered_mountd.daemon.MountdService.handle_commit`: per-child commit lock,
  dirty overlay sealing, delta build, pack verification, atomic manifest
  generation bump, and sealed-overlay cleanup.
- `ccc-layered commit`: control-socket command for manual commits.

Example:

```bash
ccc-pack build ./tree .scratch/packs/tree.sqfs \
  --manifest .scratch/registry/tree.toml \
  --child-id dataset:tree --name tree

ccc-pack verify .scratch/packs/tree.sqfs --sha256 <hex> --size <bytes>
ccc-pack manifest show .scratch/registry/tree.toml
```

**Still out:** managed parent FUSE, namespace auto-registration, real writable
union mounting, auto-commit/compaction workers, S3 mirroring/recall,
external-HPC flows, and the full privileged/FUSE/Docker CI matrix.

## License

MIT — see [`LICENSE`](LICENSE).
