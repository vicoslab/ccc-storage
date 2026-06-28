"""Tiny safe conda/mamba wrappers for CCC layered envs.

The wrappers are intentionally conservative.  They never make normal conda/mamba
unusable: unmanaged commands, missing config, missing mountd/shared state, or an
explicit disable flag all pass through to the real tool.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from ccc_layered_core.observe import OBSERVE_MARKER_NAME
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.env_txn import (
    CommandResult,
    EnvTransaction,
    EnvTxnResult,
    EnvUpdateContext,
)

EnvMap = Mapping[str, str]
RealRunner = Callable[[list[str], EnvMap], int]
ManagedRunner = Callable[[EnvUpdateContext, list[str]], CommandResult]

_MUTATING = {"install", "update", "upgrade", "remove", "uninstall"}
_NON_MUTATING = {"info", "list", "search", "config", "run", "clean", "doctor"}


def init_conda_envs(path: str | Path) -> Path:
    """Create a conda-env observation root marker idempotently."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / OBSERVE_MARKER_NAME
    marker.touch(exist_ok=True)
    return marker


def _is_truthy(value: str | None) -> bool:
    return bool(value and value.lower() in {"1", "true", "yes", "on"})


def _is_mutating(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] in _MUTATING:
        return True
    if argv[:2] == ["env", "update"]:
        return True
    if argv[0] in _NON_MUTATING:
        return False
    return False


def _extract_selector(argv: list[str], env: EnvMap) -> str:
    explicit = env.get("CCC_LAYERED_ENV_SELECTOR", "").strip()
    if explicit:
        return explicit
    for idx, arg in enumerate(argv):
        if arg in {"-n", "--name"} and idx + 1 < len(argv):
            return argv[idx + 1]
        if arg.startswith("--name="):
            return arg.split("=", 1)[1]
        if arg in {"-p", "--prefix"} and idx + 1 < len(argv):
            return Path(argv[idx + 1]).name
        if arg.startswith("--prefix="):
            return Path(arg.split("=", 1)[1]).name
    conda_prefix = env.get("CONDA_PREFIX", "").strip()
    if conda_prefix:
        return Path(conda_prefix).name
    return ""


def _default_real_runner(argv: list[str], env: EnvMap) -> int:
    proc = subprocess.run(argv, env=dict(env), check=False)
    return proc.returncode


def _default_managed_runner(ctx: EnvUpdateContext, argv: list[str]) -> CommandResult:
    proc = subprocess.run(argv, check=False)
    return CommandResult(returncode=proc.returncode)


def _shim_run_dir(env: EnvMap) -> str:
    """Return a writable node-local run dir for direct shim transactions."""
    explicit = env.get("CCC_NODE_RUN_DIR", "").strip()
    if explicit:
        return explicit
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return str(Path(runtime) / "ccc-layered")
    return f"/tmp/ccc-layered-{os.getuid()}"


def _result_exit_code(result: EnvTxnResult) -> int:
    if result.committed:
        return 0
    if result.command_returncode is not None and result.command_returncode != 0:
        return result.command_returncode
    if result.status == "blocked":
        return 75  # EX_TEMPFAIL: retryable lock contention.
    return 1


def run_shim(
    tool: str,
    argv: list[str],
    *,
    env: EnvMap | None = None,
    run_real: RealRunner | None = None,
    run_managed: ManagedRunner | None = None,
) -> int:
    """Run a conservative conda/mamba wrapper.

    Wrapping only happens when all of these are true:
    - shim is not disabled;
    - command is mutating;
    - a managed env selector can be determined;
    - ``CCC_NFS_ROOT`` is configured and contains the child manifest.

    All other cases call the real tool unchanged.
    """
    current_env: MutableMapping[str, str] = dict(os.environ if env is None else env)
    command = [tool, *argv]
    real_runner = run_real or _default_real_runner

    if _is_truthy(current_env.get("CCC_LAYERED_SHIM_DISABLE")):
        return real_runner(command, current_env)
    if not _is_mutating(argv):
        return real_runner(command, current_env)
    selector = _extract_selector(argv, current_env)
    nfs_root = current_env.get("CCC_NFS_ROOT", "").strip()
    if not selector or not nfs_root:
        return real_runner(command, current_env)

    service = MountdService(nfs_root, _shim_run_dir(current_env))
    try:
        service._find(selector)
    except KeyError:
        return real_runner(command, current_env)

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        if run_managed is not None:
            return run_managed(ctx, command)
        return _default_managed_runner(ctx, command)

    result = EnvTransaction(service, selector, runner=runner).run(command)
    return _result_exit_code(result)


def main_conda(argv: list[str] | None = None) -> int:
    return run_shim("conda", list(sys.argv[1:] if argv is None else argv))


def main_mamba(argv: list[str] | None = None) -> int:
    return run_shim("mamba", list(sys.argv[1:] if argv is None else argv))
