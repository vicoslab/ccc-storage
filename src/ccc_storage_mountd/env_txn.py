"""Phase 07 — managed conda env transaction orchestration.

A committed conda env is a clean, read-only SquashFS child (no writable overlay
in the hot import path). A package transaction (``conda/mamba/pip install`` …)
is made atomic here:

    acquire exclusive per-env update lock
      -> enable update mode (ensure the writable overlay exists)
      -> run the package-manager command (a provided command runner)
      -> on command success: run the sanity check (decode smoke)
           -> on sanity success: commit a new SquashFS generation
           -> on sanity failure: preserve the dirty overlay, no commit
      -> on command failure: preserve the dirty overlay, no commit
      -> always release the update lock

The orchestration is deliberately honest: it runs whatever command runner it is
given and only reports success/commit when that runner (and the sanity checker)
return a zero exit code. It never pretends a real package install happened. The
actual commit reuses :meth:`MountdService.handle_commit` so there is exactly one
seal -> build -> verify -> publish code path (shared with phases 03/05).

The exclusive update lock (deferred-Q4 = yes) is separate from the per-child
``.commit.lock`` taken inside ``handle_commit``, so committing at the end of a
transaction does not deadlock. Reads of the current committed generation are
never blocked by an update (D-22) — the lock only serializes *writers*.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ccc_storage_core.locks import LockHeld, NFSLock
from ccc_storage_core.manifest import ChildManifest
from ccc_storage_mountd.overlay import dirty_stats, ensure_active_upper

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from ccc_storage_mountd.daemon import MountdService

# Transaction outcome statuses (stable strings, used by the CLI and protocol).
STATUS_COMMITTED = "committed"
STATUS_COMMAND_FAILED = "command_failed"
STATUS_SANITY_FAILED = "sanity_failed"
STATUS_BLOCKED = "blocked"


@dataclass(frozen=True)
class CommandResult:
    """The outcome of a package-manager command or a sanity check."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class EnvUpdateContext:
    """What a command runner / sanity checker is handed for one transaction."""

    env_id: str
    manifest: ChildManifest
    active_upper: Path
    argv: tuple[str, ...]


# A runner executes the package-manager command for an env update and reports
# its exit code; a sanity checker validates the env post-install (decode smoke).
CommandRunner = Callable[[EnvUpdateContext], CommandResult]
SanityChecker = Callable[[EnvUpdateContext], CommandResult]


def default_sanity_checker(ctx: EnvUpdateContext) -> CommandResult:
    """The permissive default: trust the package manager's own exit code.

    Real envs override this with an actual decode smoke (``python -c "import
    <key pkgs>"``); the foundation keeps it injectable and side-effect free.
    """
    return CommandResult(returncode=0)


@dataclass(frozen=True)
class EnvTxnResult:
    """The result of an :class:`EnvTransaction` run."""

    env: str
    status: str
    committed: bool
    overlay_preserved: bool
    generation: int
    command_returncode: int | None = None
    sanity_ok: bool | None = None
    command: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "status": self.status,
            "committed": self.committed,
            "overlay_preserved": self.overlay_preserved,
            "generation": self.generation,
            "command_returncode": self.command_returncode,
            "sanity_ok": self.sanity_ok,
            "command": list(self.command),
            "message": self.message,
        }


class EnvTransaction:
    """Orchestrate one safe package transaction against a managed env."""

    def __init__(
        self,
        service: MountdService,
        selector: str,
        *,
        runner: CommandRunner,
        sanity_check: SanityChecker | None = None,
        stale_after: float = 3600.0,
    ) -> None:
        self.service = service
        self.selector = selector
        self.runner = runner
        self.sanity_check = sanity_check or default_sanity_checker
        self.stale_after = stale_after

    def _lock_path(self, manifest: ChildManifest) -> Path:
        # Imported lazily to avoid a module import cycle with the daemon.
        from ccc_storage_mountd.daemon import _safe_child_name

        return self.service.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.update.lock"

    def run(self, argv: list[str]) -> EnvTxnResult:
        manifest = self.service._find(self.selector)
        command = tuple(argv)
        lock = NFSLock(self._lock_path(manifest), op="env-update", stale_after=self.stale_after)
        try:
            lock.acquire()
        except LockHeld:
            return EnvTxnResult(
                env=manifest.id,
                status=STATUS_BLOCKED,
                committed=False,
                overlay_preserved=False,
                generation=manifest.generation,
                command=command,
                message="env update lock is held by another transaction; retry later",
            )
        try:
            # Enable update mode: ensure the writable overlay exists for writes.
            paths = self.service.overlay_paths(manifest)
            active_upper = ensure_active_upper(paths)
            ctx = EnvUpdateContext(
                env_id=manifest.id,
                manifest=manifest,
                active_upper=active_upper,
                argv=command,
            )

            cmd_result = self.runner(ctx)
            if not cmd_result.ok:
                return EnvTxnResult(
                    env=manifest.id,
                    status=STATUS_COMMAND_FAILED,
                    committed=False,
                    overlay_preserved=True,
                    generation=manifest.generation,
                    command_returncode=cmd_result.returncode,
                    command=command,
                    message="package-manager command failed; dirty overlay preserved",
                )

            sanity_result = self.sanity_check(ctx)
            if not sanity_result.ok:
                return EnvTxnResult(
                    env=manifest.id,
                    status=STATUS_SANITY_FAILED,
                    committed=False,
                    overlay_preserved=True,
                    generation=manifest.generation,
                    command_returncode=cmd_result.returncode,
                    sanity_ok=False,
                    command=command,
                    message="post-install sanity check failed; dirty overlay preserved",
                )

            committed = self.service.handle_commit(manifest.id, message="conda-env-txn")
            return EnvTxnResult(
                env=manifest.id,
                status=STATUS_COMMITTED,
                committed=True,
                overlay_preserved=False,
                generation=int(committed["generation"]),
                command_returncode=cmd_result.returncode,
                sanity_ok=True,
                command=command,
                message="transaction committed",
            )
        finally:
            lock.release()


def env_status(service: MountdService, selector: str) -> dict[str, Any]:
    """Report a managed env's runtime mode.

    A clean env is read-only with no overlay in the import path; a dirty env is
    in update mode with a preserved overlay. The update-lock state is surfaced
    so callers can tell whether a transaction is in flight.
    """
    from ccc_storage_mountd.daemon import _safe_child_name

    manifest = service._find(selector)
    paths = service.overlay_paths(manifest)
    ensure_active_upper(paths)
    stats = dirty_stats(paths.active_upper)
    dirty = stats.dirty
    lock_path = service.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.update.lock"
    return {
        "id": manifest.id,
        "name": manifest.name,
        "type": manifest.type,
        "generation": manifest.generation,
        "mode": "update" if dirty else "read-only",
        "read_only": not dirty,
        "overlay": "dirty" if dirty else "none",
        "dirty": dirty,
        "update_locked": lock_path.exists(),
        "file_count": stats.file_count,
        "bytes": stats.bytes,
    }
