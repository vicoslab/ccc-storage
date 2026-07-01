# Cold storage operation

CCC storage can move committed SquashFS packs out of hot shared storage and into a
cold-storage backend. The subsystem is called **cold storage**. S3-compatible
object storage is the current backend, but mountd and manifests model it as a
backend so future providers can be added without moving the feature under the HPC
namespace.

Cold storage is not a live filesystem mount from S3. CCC still mounts local/NFS
SquashFS pack files. When a child is cold, mountd recalls the required pack files
back into hot storage, verifies them, updates the manifest, and only then mounts
or serves the child.

## User-visible model

For each managed child, mountd tracks a pack stack:

```text
committed SquashFS packs on hot shared storage
  + active writable overlay
  -> mounted folder view
```

Cold storage adds an optional backend copy of the committed packs:

```text
hot mode:
  NFS/hot storage has pack files
  cold backend may also have a mirror copy

cold/archive mode:
  cold backend has pack files and cold manifest
  NFS/hot pack files may be removed
  manifest records where to recall them from
```

The normal lifecycle is:

```text
commit dirty overlay -> new SquashFS delta pack -> optional mirror
idle child exceeds retention threshold -> archive to cold storage -> remove hot packs
later access/mount -> recall all needed packs -> verify SHA-256/size -> mount
```

## Archive vs mirror

Cold storage has two useful modes:

| Mode | Hot/NFS packs kept? | Manifest state | Main use |
|---|---:|---|---|
| Archive | No, after successful upload | `pack_state = "cold"`, `mode = "archive"` | Free hot shared-storage space for old idle data. |
| Mirror | Yes | `pack_state = "hot"`, `mode = "mirror"` | Keep a current backend copy while local CCC remains hot. |

The same S3 backend can be used for both. Archive is eviction. Mirror is sync.
HPC exchange can use mirror/sync data later, but the cold-storage subsystem does
not depend on HPC.

## Automatic recall

Mountd checks cold-storage state before paths that need pack bytes, including:

- explicit `ccc-storage mount`;
- `ccc-storage mount-tree` child-boundary mounts;
- managed-parent lazy `access`;
- observation-root lazy access.

If a manifest is cold, or if recorded hot pack files are missing, mountd:

1. takes a per-child cold-storage lock;
2. downloads every committed pack from the backend into a staging/hot pack dir;
3. verifies every file against manifest `sha256` and `size` metadata;
4. atomically publishes the recalled pack files;
5. atomically rewrites the manifest as `pack_state = "hot"`;
6. continues the original mount/access request.

If any object is missing or corrupt, recall fails without publishing a partial hot
pack stack and the child remains cold.

## Automatic archival

Mountd can periodically archive idle children. The default scan interval is one
week when cold storage is configured; the default idle threshold is 180 days.

A child is skipped when any of these are true:

- cold storage is not configured or archival is disabled;
- the child is pinned;
- the child has uncommitted dirty data;
- the child is currently mounted;
- the child is already cold;
- the child has no `last_accessed_at` metadata yet.

The last rule protects existing deployments: on the first archival scan after
upgrading, legacy manifests are marked with access metadata and skipped, instead
of immediately evicting old data.

## Configuration

Cold-storage policy is preferably configured on mountd through the TOML config
file (see `configuration.md`):

```toml
[cold_storage]
backend = "s3"
enabled = true
archive_enabled = true
prefix = "ccc-storage/cold"
interval_seconds = 604800      # one week
idle_seconds = 15552000        # 180 days
remove_hot = true              # archive evicts by default
mirror_after_commit = false

[cold_storage.s3]
bucket = "my-bucket"
endpoint_url = "https://s3.example"
region_name = "us-east-1"
addressing_style = "auto"
```

Environment variables are still supported as deployment overrides and for
legacy setups:

```bash
# Generic cold-storage controls
CCC_COLD_STORAGE_BACKEND=s3
CCC_COLD_STORAGE_ENABLED=1
CCC_COLD_STORAGE_ARCHIVE_ENABLED=1
CCC_COLD_STORAGE_PREFIX=ccc-storage/cold
CCC_COLD_STORAGE_INTERVAL_SECONDS=604800      # one week
CCC_COLD_STORAGE_IDLE_SECONDS=15552000        # 180 days
CCC_COLD_STORAGE_REMOVE_HOT=1                 # archive evicts by default
CCC_COLD_STORAGE_MIRROR_AFTER_COMMIT=0

# S3-compatible backend settings
CCC_COLD_STORAGE_BUCKET=my-bucket             # or CCC_S3_BUCKET
CCC_COLD_STORAGE_ENDPOINT=https://s3.example  # or CCC_S3_ENDPOINT
CCC_COLD_STORAGE_REGION=us-east-1             # or CCC_S3_REGION
CCC_COLD_STORAGE_ADDRESSING_STYLE=auto        # or CCC_S3_ADDRESSING_STYLE
```

`CCC_COLD_STORAGE_ENABLED` and `CCC_COLD_STORAGE_ARCHIVE_ENABLED` default to true
only when a bucket and endpoint are configured. Set either to `0` to disable the
feature or the automatic archival pass explicitly.

Credentials are intentionally not represented in manifests or docs. Use the
standard environment/credential mechanism supported by the S3 client in the
mountd container.

The mountd scan cadence can also be set with the CLI flag:

```bash
ccc-storage mountd --cold-storage-interval 604800 ...
```

A value `<=0` disables the periodic automatic archival scan.

## CLI

Normal operation is automatic, but the CLI provides explicit status, archive, and
recall operations for operators.

Show cold-storage state:

```bash
ccc-storage cold status dataset:photos --json
```

Archive a clean, unmounted child and evict hot packs after successful upload:

```bash
ccc-storage cold archive dataset:photos --json
```

Mirror committed packs to cold storage while keeping hot/NFS packs present:

```bash
ccc-storage cold archive dataset:photos --keep-hot --json
```

Recall a cold child explicitly:

```bash
ccc-storage cold recall dataset:photos --json
```

`ccc-storage status <child> --json` also includes a `cold_storage` object with
fields such as:

```json
{
  "configured": true,
  "enabled": true,
  "archive_enabled": true,
  "backend": "s3",
  "mode": "archive",
  "pack_state": "cold",
  "snapshot_state": "available",
  "uri": "ccc-storage/cold/children/dataset-photos/g0002",
  "last_accessed_at": "2026-07-01T00:00:00Z",
  "hot_pack_files_present": false,
  "needs_recall": true
}
```

## Manifest state

New manifests write a generic `[cold_storage]` table:

```toml
[cold_storage]
backend = "s3"
mode = "archive"
pack_state = "cold"
snapshot_state = "available"
pack_generation = 2
mirror_generation = 2
overlay_generation = 0
uri = "ccc-storage/cold/children/dataset-photos/g0002"
archived_at = "2026-07-01T00:00:00Z"
last_accessed_at = "2026-07-01T00:00:00Z"
```

Older manifests with `[s3]` are still loaded for compatibility, but new writes use
`[cold_storage]`.

## Safety and limitations

- Cold storage archives committed SquashFS packs only. Commit dirty data first.
- Archive eviction refuses mounted children because hot pack files may still be in
  active use.
- Recall downloads whole pack files; it is not lazy range reading directly from
  S3.
- Corrupt or missing backend objects abort recall before any partial hot stack is
  published.
- Base/large pack recall can move a lot of data; use log-structured compaction
  and child boundaries to keep hot/cold pack units reasonable.

## Validation

Development validation scripts live under `dev/validation/s3/`:

```bash
dev/validation/s3/s3-smoke.sh
dev/validation/s3/s3-cold-hpc-smoke.sh
```

The first validates the object-store backend and primitive mirror/recall path.
The second is an integrated smoke that commits dirty data through mountd, archives
the committed pack stack, removes hot packs, recalls them, and verifies the
recalled data. The script also exercises S3 exchange artifacts used by later HPC
integration, but cold-storage archive/recall itself is independent of HPC.
