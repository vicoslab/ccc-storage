"""``ccc-storage env-*`` subcommands: managed conda env transactions (Phase 07).

These commands run *locally* against a node-built :class:`MountdService` (the
package-manager command must execute on the node where the env lives, near the
mount), rather than over the control socket. The command runner and sanity
checker are injectable so unit tests can drive the full lock → update → commit
lifecycle with a fake runner — no real conda/mamba/pip ever runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any

from ccc_storage_mountd.daemon import MountdService
from ccc_storage_mountd.env_txn import (
    CommandResult,
    EnvTransaction,
    EnvUpdateContext,
    SanityChecker,
    env_status,
)


def _build_service() -> MountdService:
    nfs_root = os.environ.get("CCC_NFS_ROOT", "")
    if not nfs_root:
        raise SystemExit("ccc-storage env: $CCC_NFS_ROOT is required for env transactions")
    run_dir = os.environ.get("CCC_NODE_RUN_DIR", "/run/ccc-storage")
    service = MountdService(nfs_root, run_dir)
    service.reload_registry()
    return service


def default_command_runner(ctx: EnvUpdateContext) -> CommandResult:
    """Run the package-manager command as a real subprocess (node-side)."""
    proc = subprocess.run(list(ctx.argv), capture_output=True, text=True, check=False)
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _emit(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for key, value in result.items():
        print(f"{key}: {value}")


def run_env_txn(
    selector: str,
    command: list[str],
    *,
    service: MountdService | None = None,
    runner: Any = None,
    sanity_check: SanityChecker | None = None,
) -> dict[str, Any]:
    """Run one env transaction and return its result dict (test entry point)."""
    svc = service or _build_service()
    txn = EnvTransaction(
        svc,
        selector,
        runner=runner or default_command_runner,
        sanity_check=sanity_check,
    )
    return txn.run(command).to_dict()


def env_txn_command(
    ns: argparse.Namespace,
    *,
    service: MountdService | None = None,
    runner: Any = None,
    sanity_check: SanityChecker | None = None,
) -> int:
    command = list(getattr(ns, "command", []) or [])
    if command and command[0] == "--":  # argparse REMAINDER keeps the separator
        command = command[1:]
    if not command:
        print("ccc-storage env-txn: no command given (use: env-txn <env> -- <cmd...>)")
        return 2
    result = run_env_txn(
        ns.path, command, service=service, runner=runner, sanity_check=sanity_check
    )
    _emit(result, as_json=getattr(ns, "json", False))
    return 0 if result["status"] == "committed" else 1


def env_status_command(
    ns: argparse.Namespace,
    *,
    service: MountdService | None = None,
) -> int:
    svc = service or _build_service()
    result = env_status(svc, ns.path)
    _emit(result, as_json=getattr(ns, "json", False))
    return 0


def add_parsers(sub: Any) -> None:
    """Register the ``env-txn`` / ``env-status`` subcommands on the CLI."""
    txn = sub.add_parser(
        "env-txn",
        help="run a managed conda env package transaction (lock -> update -> commit)",
    )
    txn.add_argument("path", help="env child name/selector")
    txn.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="-- <cmd...> package-manager command to run inside the env",
    )
    txn.add_argument("--json", action="store_true")
    txn.set_defaults(func=env_txn_command)

    status = sub.add_parser(
        "env-status",
        help="report a managed conda env's read-only/update mode and overlay state",
    )
    status.add_argument("path", help="env child name/selector")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=env_status_command)
