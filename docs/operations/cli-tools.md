# CLI tools reference

`ccc-storage` exposes one public executable:

```bash
ccc-storage <command> [args...]
```

The command has two layers:

- Tool namespaces: `pack`, `mountd`, `hpc`, `conda`, `mamba`, and `benchmark`.
- Direct mountd-control operations: `doctor`, `status`, `mount`, `commit`, `compact`, `observe-ls`, `init-conda-envs`, and the other control verbs listed by top-level help.

Regenerate/check help locally from the repository root with:

```bash
PYTHONPATH=src python -m ccc_storage.main --help
PYTHONPATH=src python -m ccc_storage.main <command> --help
```

For an editable install, use the installed executable instead:

```bash
ccc-storage --help
ccc-storage pack --help
```

## Summary

| Command | Job |
|---|---|
| `ccc-storage <control-op>` | Run unprivileged user/operator operations that talk to mountd over the Unix control socket. |
| `ccc-storage mountd` | Run the per-node privileged mount/control daemon, including managed parents, observation roots, dirty publishing, and background compaction. |
| `ccc-storage pack` | Build, verify, and inspect immutable SquashFS packs and child manifests. |
| `ccc-storage hpc` | External-HPC packset client foundation; current runtime mount/push adapters are placeholders. |
| `ccc-storage conda` | Transparent conda shim: mutating managed-env commands run under CCC env transaction/commit; unmanaged or non-mutating commands pass through to real conda. |
| `ccc-storage mamba` | Transparent mamba shim with the same CCC managed-env transaction behavior as `ccc-storage conda`. |
| `ccc-storage benchmark` | Generate deterministic small-file write/read workloads and report JSON performance metrics. |

## Help output

### `ccc-storage`

Captured command: `PYTHONPATH=src python -m ccc_storage.main --help`

```text
usage: ccc-storage [-h] [--version] {pack,mountd,hpc,conda,mamba,benchmark,doctor,status,mount,mount-tree,umount,publish,commit,compact,pin,write-policy,ls,parent-ls,create,rmdir,access,observe-ls,observe-mkdir,observe-access,rename,env-txn,env-status,init-conda-envs,import,hpc-export} ...

Unified CCC storage CLI.

options:
  -h, --help   show this help message and exit
  --version    show program's version number and exit

tool namespaces:
  pack       build, verify, and inspect immutable SquashFS packs
  mountd     run the per-node privileged mount/control daemon
  hpc        stage external-HPC packsets and import/export deltas
  conda      run the conservative managed-env conda shim
  mamba      run the conservative managed-env mamba shim
  benchmark  run deterministic small-file write/read benchmarks

control operations through mountd:
  doctor           mountd control operation
  status           mountd control operation
  mount            mountd control operation
  mount-tree       mountd control operation
  umount           mountd control operation
  publish          mountd control operation
  commit           mountd control operation
  compact          mountd control operation
  pin              mountd control operation
  write-policy     mountd control operation
  ls               mountd control operation
  parent-ls        mountd control operation
  create           mountd control operation
  rmdir            mountd control operation
  access           mountd control operation
  observe-ls       mountd control operation
  observe-mkdir    mountd control operation
  observe-access   mountd control operation
  rename           mountd control operation
  env-txn          mountd control operation
  env-status       mountd control operation
  init-conda-envs  mountd control operation
  import           mountd control operation
  hpc-export       mountd control operation

examples:
  ccc-storage doctor
  ccc-storage pack build SRC OUT.sqfs
  ccc-storage mountd --nfs-root /storage/.ccc-storage --run-dir /run/ccc-storage
  ccc-storage conda install -n env numpy

Use `ccc-storage <command> --help` for command-specific options.
```
### Direct control operations through running mountd

Control operations are invoked directly under `ccc-storage`, for example:

```bash
ccc-storage doctor
ccc-storage status observe:my-env --json
ccc-storage commit observe:my-env -m "updated env"
ccc-storage compact observe:my-env --dry-run --json
```

Subcommand-specific help is available for each direct operation:

```text
usage: ccc-storage status [-h] [--json] path

positional arguments:
  path

options:
  -h, --help  show this help message and exit
  --json
```

### `ccc-storage mountd`

Captured command: `PYTHONPATH=src python -m ccc_storage.main mountd --help`

```text
usage: ccc-storage mountd [-h] [--version] [--probe] [--nfs-root NFS_ROOT]
                          [--run-dir RUN_DIR] [--socket SOCKET]
                          [--prefer-kernel] [--socket-mode SOCKET_MODE]
                          [--managed-parent MANAGED_PARENT]
                          [--observe-root OBSERVE_ROOT]
                          [--observe-mountpoint OBSERVE_MOUNTPOINT]
                          [--default-write-policy {local-ssd-async,shared-nfs}]
                          [--local-overlay-root LOCAL_OVERLAY_ROOT]
                          [--dirty-publish-interval DIRTY_PUBLISH_INTERVAL]
                          [--compaction-interval COMPACTION_INTERVAL]
                          [--observe-ready-timeout OBSERVE_READY_TIMEOUT]
                          [--ready-file READY_FILE]
                          [--idle-unmount-ttl IDLE_UNMOUNT_TTL]
                          [--idle-reap-interval IDLE_REAP_INTERVAL]
                          [--once-doctor]

Per-node layered-storage daemon.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --probe               print runtime-ingredient summary
  --nfs-root NFS_ROOT
  --run-dir RUN_DIR
  --socket SOCKET
  --prefer-kernel       prefer kernel mount(2) helpers over FUSE helpers where
                        supported
  --socket-mode SOCKET_MODE
                        octal permissions for the control socket (default:
                        0600)
  --managed-parent MANAGED_PARENT
                        managed parent path whose children this node serves
                        (e.g. /managed/dataset)
  --observe-root OBSERVE_ROOT
                        source tree whose CCC_STORAGE_OBSERVE markers define
                        observed children
  --observe-mountpoint OBSERVE_MOUNTPOINT
                        mount a live pyfuse3 observation dispatcher at this
                        path
  --default-write-policy {local-ssd-async,shared-nfs}
                        write policy for new children when observe marker has
                        no explicit policy
  --local-overlay-root LOCAL_OVERLAY_ROOT
                        node-local SSD root for local-ssd-async upper/work
                        dirs
  --dirty-publish-interval DIRTY_PUBLISH_INTERVAL
                        seconds between best-effort local-ssd-async dirty
                        mirror publishes
  --compaction-interval COMPACTION_INTERVAL
                        seconds between safe background log-structured
                        compaction passes; <=0 disables
  --observe-ready-timeout OBSERVE_READY_TIMEOUT
                        seconds to wait for --observe-mountpoint before
                        serving
  --ready-file READY_FILE
                        write doctor JSON here once the socket is accepting
                        requests
  --idle-unmount-ttl IDLE_UNMOUNT_TTL
                        seconds before idle refcount-zero child mounts are
                        unmounted; <=0 disables
  --idle-reap-interval IDLE_REAP_INTERVAL
                        seconds between idle-mount cleanup passes
  --once-doctor         print doctor JSON and exit
```
### `ccc-storage pack`

Captured command: `PYTHONPATH=src python -m ccc_storage.main pack --help`

```text
usage: ccc-storage pack [-h] [--version] {build,verify,manifest} ...

Build and inspect CCC immutable SquashFS packs.

positional arguments:
  {build,verify,manifest}
    build               build a SquashFS pack from a source directory
    verify              verify a pack checksum/size
    manifest            manifest operations

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```


### `ccc-storage hpc`

Captured command: `PYTHONPATH=src python -m ccc_storage.main hpc --help`

```text
usage: ccc-storage hpc [-h] [--version] {status,mount,push} ...

External-HPC packset client foundation.

positional arguments:
  {status,mount,push}
    status             show staged packset status
    mount              stage/mount a packset bundle (runtime FUSE adapter
                       pending)
    push               push output delta to import queue (runtime adapter
                       pending)

options:
  -h, --help           show this help message and exit
  --version            show program's version number and exit
```

### `ccc-storage conda` and `ccc-storage mamba`

These wrappers intentionally pass through to the underlying package manager when
wrapping is disabled, the target is unmanaged, shared state is absent, or the
command is non-mutating. Invoke them as:

```bash
ccc-storage conda install -n my-env numpy
ccc-storage mamba update -n my-env --all
```

Package-manager help is also pass-through, so `ccc-storage conda --help` and
`ccc-storage mamba --help` display the installed conda/mamba help from the local
environment.

### `ccc-storage benchmark`

Captured command: `PYTHONPATH=src python -m ccc_storage.main benchmark --help`

```text
usage: ccc-storage benchmark [-h] --root ROOT --target TARGET --workload-name
                             WORKLOAD_NAME --files FILES
                             [--size-bytes SIZE_BYTES] [--size-kib SIZE_KIB]
                             [--size-mib SIZE_MIB] [--fanout FANOUT]
                             [--prefix PREFIX] [--suffix SUFFIX] [--seed SEED]
                             [--chunk-mib CHUNK_MIB] [--json-out JSON_OUT]
                             [--no-sync] [--no-clean]

Write/read performance benchmark helpers for CCC layered storage.

options:
  -h, --help            show this help message and exit
  --root ROOT
  --target TARGET
  --workload-name WORKLOAD_NAME
  --files FILES
  --size-bytes SIZE_BYTES
  --size-kib SIZE_KIB
  --size-mib SIZE_MIB
  --fanout FANOUT
  --prefix PREFIX
  --suffix SUFFIX
  --seed SEED
  --chunk-mib CHUNK_MIB
  --json-out JSON_OUT
  --no-sync
  --no-clean
```
