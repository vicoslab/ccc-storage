# Conda/mamba shim

The package provides conservative wrappers:

```bash
ccc-storage conda ...
ccc-storage mamba ...
```

They are safe to install in CCC images because they pass through to real
`conda`/`mamba` unless a managed layered environment is explicitly detectable.

## Fallback behavior

The wrapper runs the real tool unchanged when:

- `CCC_STORAGE_SHIM_DISABLE=1`
- the command is read-only, such as `list`, `info`, `search`, `config`, or `run`
- no managed env selector is available
- `CCC_NFS_ROOT` is not configured
- the selected env is not registered as a layered env

This means normal conda/mamba use still works if mountd is not present.

## Managed transaction behavior

For mutating commands on a managed env, the wrapper runs one atomic transaction:

```text
update lock -> writable overlay -> package command -> sanity check -> SquashFS commit
```

Covered mutating commands initially:

- `install`
- `update` / `upgrade`
- `remove` / `uninstall`
- `env update`

Selector discovery order:

1. `CCC_STORAGE_ENV_SELECTOR`
2. `-n/--name`
3. `-p/--prefix`
4. `CONDA_PREFIX` basename

If the package command fails, the dirty overlay is preserved and the wrapper
returns the real command exit code.  Lock contention returns `75` (`EX_TEMPFAIL`).

## Mark a conda env root for observation

```bash
ccc-storage init-conda-envs /storage/user/layered-source/conda/envs
```

This creates the `CCC_STORAGE_OBSERVE` marker so mountd can treat immediate child
folders as independently committed layered envs.

## Optional transparent use

Do not overwrite real `conda`/`mamba` by default.  If transparent behavior is
wanted, place tiny shell wrappers named `conda`/`mamba` in an admin-controlled
shim directory that calls `ccc-storage conda`/`ccc-storage mamba`, then prepend only that
directory to `PATH` in selected CCC images.
