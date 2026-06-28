# ccc-layered-storage

SquashFS-backed **layered storage** for the CCC compute cluster — a monorepo of
five independently-buildable Python packages plus a shared test harness.

> **Current status — Phase 13 live marker-observation FUSE dispatcher.**
> Phase 00 created the safe dev/test harness. Phase 01 added immutable pack
> metadata, TOML manifests, locks, checksums, and `ccc-pack`. Phase 02 added the
> newline-JSON control protocol, node-local `MountdService`, Unix control
> socket, and child-mount refcounting abstraction. Phase 03 added shared-overlay
> directory bookkeeping, dirty stats, active→sealed overlay rotation, manual
> commit locking, delta-pack publication, and `ccc-layered commit`. Phase 04
> added the service-level managed-parent namespace (`list`/`create`/`rename`/
> `rmdir`/lazy `access`), lock-guarded atomic child creation, lazy-mount with an
> idle-unmount reaper, and a documented pyfuse3 dispatcher placeholder. Phase 05
> adds the deterministic auto-commit policy engine (D-12), the delta-pack
> compaction planner with a safe build/publish skeleton (D-11), the conservative
> retention/GC planner (deferred-Q7), `ccc-layered pin`, and enriched `status`
> (dirty stats, policy decision, delta count, compaction state). Phase 06 adds the
> hierarchical/nested pack foundation (D-13/D-14/D-15): a shared longest-prefix
> boundary resolver, a bidirectional parent<->child `BoundaryRegistry`, parent
> builds that exclude child subtrees and emit hidden boundary markers,
> per-boundary overlay write-routing, lazy nested submounts with an idle reaper
> that bounds the active-submount count, and boundary-aware commit-owner
> resolution. Phase 07 applies the layered model to the conda-env workload: a
> clean env is a read-only SquashFS child (no overlay in the import path) while a
> package transaction takes an exclusive per-env update lock, writes to the
> overlay, runs a sanity check, then commits-on-success / preserves-on-failure
> (`ccc-layered env-txn` / `env-status`). Phase 08 adds a no-network object-store
> abstraction, best-effort pack/manifest mirroring, verified cold recall, packset
> bundles with mount graphs/checksums, minimal closure computation with explicit
> excluded-child stubs, a staged HPC client lookup model, and review-branch import
> queue / mocked HPC-run orchestration. Real S3/SSH/SLURM/FUSE runtime adapters
> remain later phases. Phase 09 adds the full CI matrix skeleton, coverage gate,
> skip-with-reason conditional lanes, optional test Dockerfile, and safe host
> deployment artifacts (`deploy/ccc-layered-mountd.service`, install/uninstall,
> node prerequisites). Later runtime validation added real committed-stack
> remount checks, real S3 cold-tier/HPC-exchange smokes, and explicit
> hierarchical pack mountpoint validation. Phase 12 replaces bespoke explicit
> nesting as the preferred model with visible `CCC_LAYERED_OBSERVE` markers:
> any marked directory is an observation/interception root, every immediate
> subdirectory below it is an independent SquashFS+overlay child namespace, and
> `observe-mkdir`/`observe-access` model the future FUSE dispatcher's lazy
> registration/access behavior without mounting all children upfront. Phase 13
> adds the live pyfuse3 observation dispatcher: `mkdir` creates generation-0
> writable child views, repeated lookups are idempotent automount triggers,
> conservative `rmdir`/`rename` only operate on clean unmounted generation-0
> children, and the privileged Docker/FUSE smoke validates write → commit →
> unmount/remount against real `fuse-overlayfs`.

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
| `ccc-layered-mountd` | `ccc_layered_mountd.daemon:main` | implemented: control socket + manifest/status/mount + managed-parent (`--managed-parent`) + marker observation roots (`--observe-root`) |
| `ccc-layered` | `ccc_layered_cli.main:main` | implemented: `doctor`, `status`, `ls`, `mount`, `umount`, `commit`, `pin`, `parent-ls`, `create`, `rename`, `rmdir`, `access`, `observe-ls`, `observe-mkdir`, `observe-access` |
| `ccc-layered-hpc` | `ccc_layered_hpc.client:main` | foundation: `status`, explicit runtime-adapter stubs for `mount`/`push` |

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
| `CCC_OBSERVE_ROOT` | Source tree whose `CCC_LAYERED_OBSERVE` marker files define observation roots | unset |

### Marker Observation Roots

Explicit nested child-boundary manifests are now a compatibility layer, not the
preferred authoring model. Place the visible marker file `CCC_LAYERED_OBSERVE`
inside any directory that should become an observation/interception root. Every
immediate subdirectory under that marker is an independent child mountpoint with
its own manifest and `pack_object_dir` namespace. Observation roots can be
recursive: a marker at `/storage/main` makes `/storage/main/user1` a child, and a
marker at `/storage/main/user1/conda` makes
`/storage/main/user1/conda/<env>` children inside the `user1` child. When paths
overlap, the nearest/deepest observation root wins; sibling prefixes do not
match.

Pack builds can use `exclude_observed=True` to derive child boundaries directly
from marker files. The parent pack keeps marker files and empty mountpoint stubs
but excludes payload below observed child mountpoints. Runtime observation is
lazy: `observe-mkdir` registers a child manifest and overlay without mounting
it; `observe-access` mounts only the requested child. The live pyfuse3 dispatcher
serves the observation root as a transparent directory: `mkdir` below the root
creates a generation-0 writable child, the first lookup/opendir below a child
triggers a node-local mount in the background, and commit publishes the overlay
as the next SquashFS delta generation.

Privileged runtime validation requires Docker, `/dev/fuse`, and bind access to a
scratch runtime root:

```bash
CCC_RUNTIME_KEEP=1 \
CCC_OBSERVATION_FUSE_TIMEOUT=300 \
CCC_RUNTIME_DOCKER_SOURCE_ROOT=/opt/shared_storage/user_data/<user>/ccc-layered-storage-observation-fuse-test \
deploy/observation-fuse-runtime-smoke.sh
```

The smoke builds the deploy image with `.[fuse]`, starts `ccc-layered-mountd`
with `--observe-mountpoint`, validates generation-0 write through the live FUSE
view, commits to SquashFS, remounts, and reads the committed payload back.

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

**Phase 04 complete:**

- `ccc_layered_mountd.managed_parent.ManagedParent`: pure, unit-testable
  managed-parent namespace logic — `list_children` (hides internal/marker
  files), lock-guarded atomic `create_child` (generation-0 child manifest +
  initialized overlay, exactly one winner under concurrent create),
  atomic `rename_child`, policy-guarded `remove_child` (refuses committed /
  non-empty children with a clear `ccc-layered`-referencing error; removes only
  empty generation-0 children), and lazy `access_child`. It is a *shallow*
  control-plane layer only and never serves file bytes (RK-7).
- `ccc_layered_mountd.childmount`: lazy-mount support — a `clock`-injectable
  manager, `release` (drop a handle without unmounting), and
  `idle_unmount_expired` (TTL reaper that never unmounts a child with refcount
  > 0).
- `ccc_layered_mountd.dispatcher_fuse`: documented adapter placeholder that
  imports pyfuse3 lazily and refuses clearly (`DispatcherUnavailable`) when it
  is unavailable or unimplemented — unit tests never require real FUSE.
- `ccc-layered-mountd --managed-parent` plus `ccc-layered parent-ls / create /
  rename / rmdir / access` over the control socket.

**Phase 05 complete:**

- `ccc_layered_mountd.workers.policy`: pure, deterministic auto-commit policy
  engine (D-12) — `CommitPolicy` thresholds (≥1 GiB after a 10-min quiet period,
  ≥100k changed files forces a commit, weekly small-dirty cadence), a
  `PolicyInputs` snapshot, `evaluate()` returning `trigger`/`manual`/`noop`, and
  `overlay_inputs()` (clock-injectable dirty accounting — no hot-path scans).
- `ccc_layered_mountd.workers.auto_commit.AutoCommitWorker`: `tick()` evaluates
  every child and reuses `MountdService.handle_commit` for the phase-03 sealed-gen
  commit; honors per-child manual-only mode and skips gracefully when the commit
  lock is held (never races a manual commit). Deterministic `poke()`/`tick()`
  hooks, no wall-clock sleeps.
- `ccc_layered_mountd.workers.compaction`: D-11 planner (`plan_compaction`) that
  triggers on **>8 deltas OR delta bytes >20% of base**, plus a safe
  build→verify→publish→retire skeleton (`consolidate` requires a *materialized*
  layered source dir; `publish_consolidation` swaps the stack and returns retired
  packs for the GC planner). Real layered merge needs a mounted union (later).
- `ccc_layered_mountd.workers.gc.plan_gc`: conservative retention (deferred-Q7) —
  evicts a retired pack only with no dirty overlay, no active mount, no pending
  commit lock, and not `pinned`; `admin_override` bypasses the mount/dirty/lock
  predicates but never a pin (RK-9).
- `ccc_layered_core.manifest`: backward-compatible `pinned` and `commit_mode`
  fields (emitted only when non-default).
- `MountdService.handle_pin` + the `pin` control command; enriched `status`
  (`pinned`, `delta_count`, `policy` decision, `compaction` state).
- `ccc-layered pin <child> [--clear]`.

Example:

```bash
ccc-pack build ./tree .scratch/packs/tree.sqfs \
  --manifest .scratch/registry/tree.toml \
  --child-id dataset:tree --name tree

ccc-pack verify .scratch/packs/tree.sqfs --sha256 <hex> --size <bytes>
ccc-pack manifest show .scratch/registry/tree.toml
```

**Phase 06 complete:**

- `ccc_layered_core.resolve`: the shared longest-prefix boundary resolver —
  `resolve_owner_path()` / `OwningBoundary` map an absolute/relative path to its
  nearest owning boundary (or the parent), used identically by overlay routing,
  commit-owner selection, and (later) HPC export. Sibling-prefix safe
  (`conda/envs/env-a2` is *not* owned by `conda/envs/env-a`); deeper boundaries
  win over shallower ones.
- `ccc_layered_core.resolve.BoundaryRegistry`: bidirectional lookup built from
  manifests — `boundaries_of(parent)`, `parent_of(child)` (`ParentRef`), and
  `resolve_owner(parent, path)`.
- `ccc_layered_pack.builder`: `plan_boundary_markers` / `create_boundary_markers`
  emit navigation stubs (empty boundary dir + an internal `.ccc-boundary` marker)
  for excluded child subtrees without copying child payload; `build_pack(...,
  exclude_boundaries=[...])` / `count_files` keep child contents out of the parent
  pack (no duplication — D-13).
- `ccc_layered_mountd.managed_parent.visible_entries`: reuses `is_internal_name`
  to hide boundary markers from listings while keeping normal boundary names
  (`env-a`) visible (RK-8).
- `ccc_layered_mountd.overlay.route_path` / `OverlayRoute`: a write under a child
  boundary routes to the child's overlay; a write outside routes to the parent
  overlay.
- `ccc_layered_mountd.childmount.NestedMountManager`: lazy nested submounts
  (Option A) — `access_child` mounts only the touched child, `idle_reap` unmounts
  idle children while the parent view stays mounted, and `active_child_count`
  bounds the active-submount count (100-boundary ceiling test). `ChildMountManager`
  gains `active_ids`/`active_count`; mountd `doctor` reports
  `active_submount_count`.

**Phase 07 complete:**

- `ccc_layered_mountd.env_txn.EnvTransaction`: managed conda env transaction
  orchestration — acquire an exclusive per-env update lock
  (`<nfs-root>/locks/<env>.update.lock`, deferred-Q4 = yes) → enable update mode
  (ensure the writable overlay) → run a provided package-manager command runner →
  on command success run a sanity check (decode smoke) → commit a new SquashFS
  generation on success, or **preserve the dirty overlay** for inspection on
  command/sanity failure → always release the lock (including on exceptions). A
  second concurrent transaction gets a clear `blocked` result; the commit reuses
  the phase-03/05 `handle_commit` seal→build→verify→publish path (the separate
  `.commit.lock` means no deadlock), and reads of the current committed
  generation are never blocked by an update (D-22).
- `CommandRunner` / `SanityChecker` are injectable callables receiving an
  `EnvUpdateContext` (env id, manifest, active-upper path, argv); the wrappers are
  honest — they report success/commit only when the runner returns exit 0 and
  never simulate a real package install. `pip install -e` editable markers and any
  other writes land in the overlay and are committed on success like any other.
- `ccc_layered_mountd.env_txn.env_status`: a clean env reports
  `mode=read-only`/`overlay=none` (no overlay in the import path); a dirty env
  reports `mode=update`.
- `ccc-layered env-txn <env> -- <cmd...>` and `ccc-layered env-status <env>`:
  node-local CLI entry points (the pm command runs on the node near the mount).

**Phase 08 complete:**

- `ccc_layered_hpc.object_store.LocalObjectStore`: deterministic no-network
  object-store abstraction for tests and future S3 adapters.
- `ccc_layered_hpc.s3mirror`: best-effort pack+manifest mirroring and verified
  cold recall. Recall downloads to a temp file, checks size+SHA-256, atomically
  publishes into the hot pack directory, and leaves the authoritative manifest
  untouched on corrupt/truncated objects.
- `ccc_layered_pack.bundle`: packset bundles with `manifest.json` mount graph,
  `checksums.sha256`, safe extraction, and tamper detection.
- `ccc_layered_hpc.closure.compute_mount_closure`: computes root + explicitly
  selected children and records excluded child-boundary stubs so HPC jobs fail
  clearly if they touch data not exported.
- `ccc_layered_hpc.client.StagedPackset`: mount-graph lookup core for the future
  HPC FUSE client; included children resolve, excluded children raise
  `ExcludedChildError` with a clear reason.
- `ccc_layered_hpc.importqueue.ImportQueue`: validates/copies incoming delta
  bundles onto named review branches with provenance and explicit promotion.
- `ccc_layered_hpc.hpc_run`: mocked SSH/SLURM orchestration foundation that builds
  a packset, submits through a fake transport, collects an output delta, and
  lands it in the review queue.

**Phase 09 complete:**

- `.github/workflows/ci.yml`: split always-on lanes (`lint`, `unit`,
  `fuse-unpriv`, `multinode`, `bench-smoke`) plus conditional lanes
  (`kernel-mount`, `docker-propagation`, `real-s3`) that skip with an explicit
  reason when capabilities/credentials are absent. The unit lane sets
  `$CCC_TEST_ROOT` inside the runner workspace and enforces a core+pack coverage
  gate (`--cov-fail-under=85`).
- `deploy/ccc-layered-mountd.service`: host-level systemd unit template with
  `/dev/fuse`, `CAP_SYS_ADMIN`, `Restart=on-failure`, managed-parent env vars,
  and runtime directory setup.
- `deploy/install.sh` / `deploy/uninstall.sh`: copy/remove/reload helpers with
  safe defaults — install does **not** auto-enable or auto-start the daemon.
- `deploy/PREREQS.md`: node prerequisites, degraded modes, preflight, and
  rollback notes.
- `Dockerfile`: optional dev/test image that runs `make test`; Docker remains a
  conditional lane, not a required development dependency.
- Phase-09 tests assert workflow structure, skip-with-reason lanes, coverage gate
  text, deploy artifacts, optional Dockerfile semantics, and `--version` on all
  entry points.

**Phase 12 marker-observation foundation complete:**

- `ccc_layered_core.observe`: visible `CCC_LAYERED_OBSERVE` marker discovery,
  immediate-child boundary enumeration, and deepest-observation-root path
  resolution with sibling-prefix safety. Removing the marker removes the
  observation root from discovery.
- `ccc_layered_pack.builder.build_pack(..., exclude_observed=True)`: excludes
  payload for observed child mountpoints, keeps user-visible observation marker
  files, and emits only `.ccc-boundary` stubs at child mountpoints. Root/user/env
  packs live in separate `packs/<safe-id>/` namespaces.
- `ccc_layered_mountd.observation.ObservationManager`: service-level model for
  the future FUSE dispatcher — `observe-mkdir` registers a child manifest and
  overlay without mounting it; `observe-access` lazily mounts only the requested
  committed child. Nested observation roots use the same mechanism as the root,
  not a bespoke explicit nesting path.
- `deploy/observation-runtime-smoke.sh`: privileged Docker/FUSE smoke that builds
  root, user, and env packs from visible markers, verifies parent packs exclude
  child payload by `unsquashfs`, and proves lazy access mounts only the touched
  nested child.

**Still out:** real conda/mamba/pip package installs and the conda transparency
bucket (hardlink/symlink survival, baked shebangs, binary relocation, real `pip
-e` round-trip) — gated behind the FUSE/real-runtime lanes (RK-8); the real
pyfuse3 managed-parent/observation-root dispatcher binding that intercepts live
POSIX `mkdir`/lookup/readdir on the mounted root (mount propagation into
containers, hot-path latency gate), real writable union mounting, real layered
compaction merge (needs a mounted union view), boundary-scoped auto-commit wiring
and child-gen pinning, real S3 backend wiring, real SSH/SLURM/HPC FUSE runtime
adapters, and live privileged/FUSE/Docker/multinode deployment validation.

## License

MIT — see [`LICENSE`](LICENSE).
