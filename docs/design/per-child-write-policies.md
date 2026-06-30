# Per-child write policies

CCC layered storage supports two writable-child backends selected per child:

| Policy | Foreground write path | Dirty visibility | Intended use |
|---|---|---|---|
| `shared-nfs` | `fuse-overlayfs` with NFS `upperdir/workdir` | immediate shared NFS upper | conservative default, general folders |
| `local-ssd-async` | kernel OverlayFS with node-local SSD `upperdir/workdir` | async logical mirror/epochs on NFS | conda envs, dataset write bursts |

The policy is stored in each child manifest as `write_policy`.  Missing legacy
values mean `shared-nfs`.

## Observation-root defaults

An observation root marker can define the default policy for newly created child
manifests below that root:

```toml
write_policy = "local-ssd-async"
```

An empty marker uses mountd's daemon default.  Per-child policy can later be
changed with CLI while the child is clean and safely remountable.

## Shared-NFS policy

`shared-nfs` is the existing implementation:

```text
SquashFS lower stack
+ NFS active upper/work
+ fuse-overlayfs
```

Properties:

- dirty writes are immediately on shared NFS;
- other nodes can observe the shared upper directly;
- writes are slower because every create/write/close crosses FUSE and NFS;
- this remains the safe default.

## Local-SSD async policy

`local-ssd-async` is optimized for write bursts:

```text
SquashFS lower stack
+ node-local SSD active upper/work
+ kernel OverlayFS
+ mountd-integrated async publisher to NFS logical mirror
```

Properties:

- foreground writes return after local SSD/kernel OverlayFS work;
- mountd is not in the per-file foreground data path;
- mountd periodically publishes complete dirty-upper epochs to NFS;
- remote readers consume only complete published epochs layered over committed
  packs, or committed packs alone when no epoch exists;
- one writer per child is allowed initially.

The publisher exports logical dirty data from the node-local active upper and
skips empty uppers.  It must not export raw kernel OverlayFS workdir internals as
the distributed state.

## Locking and conflicts

Initial rule: **single writer per child**.

- `local-ssd-async` writer mount acquires an NFS writer lock.
- A second node that cannot acquire the writer lock mounts the latest published
  logical mirror read-only, or the committed pack stack if no mirror exists.
- Mode switching requires a clean child: no shared-NFS upper dirtiness, no
  published async mirror dirtiness, and no unpublished node-local dirty upper.
- No multi-writer merging is attempted initially.

Crash behavior:

- writes already published to NFS mirror remain visible;
- writes acknowledged only to local SSD can require local recovery if the node
  dies before publication;
- stale writer lock handling should be explicit/conservative.

## Policy switching

CLI switching changes the manifest policy and optionally remounts the local node:

```bash
ccc-layered write-policy observe:env shared-nfs
ccc-layered write-policy observe:env local-ssd-async --remount
```

Switching rules:

- refuse invalid policy;
- refuse dirty children, including unpublished local-SSD upper data and
  published async mirror data;
- refuse mounted children unless `--remount` is supplied;
- remount only affects the local node; other nodes refresh on their next mount or
  explicit remount.

## Commit behavior

- `shared-nfs`: commit from the NFS active upper, as before.
- `local-ssd-async`: after the writer is drained/unmounted, commit from the
  latest non-empty published dirty mirror.  Commit refuses if a per-child writer
  lock is still held, if the mirror is empty, or if the mirror base generation no
  longer matches the manifest generation.

After a successful `local-ssd-async` commit, mountd removes the NFS async mirror
and node-local dirty upper/work state so the next mount starts from the committed
pack stack rather than re-publishing stale local files.

## Performance gates

A production runtime smoke must validate both policies:

- `shared-nfs` remains functional and no worse than previous smoke baselines;
- `local-ssd-async` write throughput is at least `4000 files/s` for the
  `2k × 32KiB` image-like workload or at least `5x` faster than shared-NFS dirty
  writes in the same run;
- SquashFS/read path remains acceptable;
- a published NFS mirror becomes visible to a second mount within a bounded
  interval and contains complete files only.

Validated runtime result on `donbot` (`deploy/validation/docker/write-policy-runtime-smoke.sh`,
Docker/FUSE, `2,000 × 32 KiB` files, run
`donbot-20260630T135531Z-41409`; artifact:
`docs/benchmarks/write-policy-smoke-donbot-20260630T135531Z.json`):

| Policy | FS type | Write files/s | Read files/s | Write time |
|---|---|---:|---:|---:|
| `shared-nfs` | `fuse.fuse-overlayfs` | `393.58` | `1666.54` | `5.082s` |
| `local-ssd-async` | kernel `overlay` | `6332.91` | `9508.59` | `0.316s` |

Measured local write speedup: `16.09×`.  The same smoke also verified explicit
policy switching, NFS dirty mirror publication, second-mount read visibility,
local commit to SquashFS delta generation `1`, and post-commit remount/read.
