# Configuration

`ccc-storage` now has a mountd-owned TOML configuration file.  The unprivileged
client CLI normally does **not** need this file: client operations are sent over
the mountd control socket and use the values/policies already loaded by mountd.

## Why TOML

TOML is the preferred format for mountd configuration here because it is:

- human-editable and comment-friendly, unlike JSON;
- available via Python 3.11 stdlib (`tomllib`) for dependency-free reads;
- section-oriented, so deployment systems can merge or template only the tables
  they own;
- explicit enough to reject typoed keys before mountd starts.

YAML would add another parser dependency and more ambiguous typing.  Python files
would make configuration executable, which is inappropriate for operator input.

## Load order and precedence

Mountd loads settings in this order:

```text
built-in defaults
  < TOML config file
  < CCC_* / compatibility environment variables
  < explicit `ccc-storage mountd ...` flags
```

The config path can be supplied as:

```bash
ccc-storage mountd --config /etc/ccc-storage/mountd.toml
```

or through:

```bash
CCC_STORAGE_MOUNTD_CONFIG=/etc/ccc-storage/mountd.toml
```

If neither is set, mountd automatically reads `/etc/ccc-storage/mountd.toml` when
that file exists.  Missing implicit default files are ignored; missing explicit
files are startup errors.

## File layout

Use `deploy/config/mountd.example.toml` as the starting point.  The intended
section boundaries are:

| Section | Purpose |
|---|---|
| `[paths]` | Runtime paths, optional legacy NFS state root, optional legacy managed-parent paths, local SSD overlay root. |
| `[[observation_dirs]]` | Primary observation directories. Each path owns `<path>/.ccc-storage` state by default. |
| `[runtime]` | Mount/runtime toggles such as `prefer_kernel`, socket mode, and observation readiness timeout. |
| `[defaults]` | Defaults applied to newly discovered/created children, currently `write_policy`. |
| `[maintenance]` | Periodic local housekeeping intervals: idle unmount, reap loop, dirty mirror publish. |
| `[ownership]` | Optional fixed UID/GID for mountd-created shared state. Configure both or neither. |
| `[compaction]` | Log-structured SquashFS level policy and background compaction interval. |
| `[cold_storage]` | Generic cold-storage policy: enabled/archive mode, prefix, retention/scan intervals, hot-pack removal. |
| `[cold_storage.s3]` | S3-compatible backend location: bucket, endpoint, region, addressing style. |

S3 access keys should stay in normal boto3 credential locations (`AWS_*`
environment variables, profiles, instance roles, or mounted secret files), not in
the TOML file.

A legacy/simple top-level `[s3]` table is also accepted for backend location
fields, but new files should prefer `[cold_storage.s3]` so the subsystem remains
backend-neutral.

## Observation directories

Normal deployments should configure observation directories instead of the legacy
`paths.nfs_root` + `paths.observe_root` + `paths.observe_mountpoint` triple:

```toml
[[observation_dirs]]
path = "/storage/user"
state_subdir = ".ccc-storage"  # default

[[observation_dirs]]
path = "/storage/datasets"
```

Mountd creates and uses:

```text
OBSERVATION_DIR/.ccc-storage/{registry,packs,overlays,locks,events}
```

Nested observation directories are allowed.  Operations below a nested root use
the nearest root's `.ccc-storage` state.  At runtime, initialize and register a
new root through mountd:

```bash
ccc-storage observe init /storage/user/user1/conda/envs
```

## Client configuration boundary

The client-side `ccc-storage` CLI should stay intentionally thin.  It may still
read client-local operational values such as:

- `CCC_MOUNTD_SOCK` for non-default socket discovery;
- `CCC_MOUNTD_REQUEST_TIMEOUT` for local request timeout tuning.

Cold-storage policy, S3 backend location, retention windows, compaction policy,
write-policy defaults, and mount/runtime paths belong to mountd.  If a client
needs those values, add a mountd control/doctor/status response rather than
requiring every client container to receive the full config file.
