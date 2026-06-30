# Log-structured Compaction Operation

CCC layered storage stores each child pack stack oldest/base first and newest
last. New commits append a small delta pack, then the level planner can compact
only the newest suffix that violates the configured level shape.

Canonical cross-system design notes live one level up in
[`../docs/components/log-structured-pack-levels.md`](../../../docs/components/log-structured-pack-levels.md)
and the implementation plan in
[`../docs/implementation/log-structured-pack-levels-implementation-plan.md`](../../../docs/implementation/log-structured-pack-levels-implementation-plan.md).

## Configuration

The daemon reads these environment variables at startup:

```bash
CCC_PACK_LEVELS=0:100G,1:10G,2:1G,3:100M,4:10M
CCC_MAX_ONLINE_COMPACTION_BYTES=10G
CCC_ALLOW_BASE_COMPACTION=0
CCC_MAX_PACKS_PER_LEVEL=1
CCC_COMPACT_AFTER_COMMIT=1
CCC_COMPACT_INTERVAL_SECONDS=0
```

`L0` is the largest/base level.  Higher numbers are smaller/newer levels.  Base
compaction is heavy maintenance; keep `CCC_ALLOW_BASE_COMPACTION=0` for routine
online service unless the node is in an explicit maintenance window.

## CLI

Preview a compaction plan:

```bash
ccc-storage compact dataset:child --dry-run --json
```

Run an online-safe compaction:

```bash
ccc-storage compact dataset:child --json
```

Allow a base rewrite for explicit maintenance:

```bash
ccc-storage compact dataset:child --allow-base --json
```

Status includes `packs[*].level`, generation metadata when present, and a
`compaction` object with `needed`, `target_level`, `selected_packs`,
`total_bytes`, and `blocked_reason`.

## Tombstones

Delta preparation preserves OverlayFS/fuse-overlayfs `.wh.*` files, including
opaque markers such as `.wh..wh..opq`.  Partial compaction carries those files
forward conservatively so deletes in upper packs do not re-expose untouched lower
content.  A future CCC-native tombstone index can replace this runtime-specific
representation; until then, do not filter `.wh.*` artifacts out of committed
delta or compacted packs.

## Runtime Tools

Partial compaction builds use `unsquashfs` to extract selected packs old-to-new
into a temporary materialized directory, then `mksquashfs` to build one target
pack.  Live FUSE is not required for this build path, but both SquashFS tools
must be installed on nodes that perform compaction.
