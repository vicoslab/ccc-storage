"""Auto-commit worker: evaluate the commit policy on a tick and commit.

The worker is deliberately thin — it owns the *policy decision* and reuses the
phase-03 sealed-gen commit (``MountdService.handle_commit``) for the actual
lock → seal → build → verify → publish sequence, so there is exactly one commit
code path. It never blocks writers (commit rotates to a fresh active upper) and
never races a manual commit: a held per-child commit lock surfaces as
``LockHeld`` and is skipped gracefully until the next tick.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ccc_storage_core.locks import LockHeld
from ccc_storage_core.manifest import ChildManifest
from ccc_storage_mountd.overlay import ensure_active_upper
from ccc_storage_mountd.workers.policy import (
    TRIGGER,
    CommitPolicy,
    evaluate,
    overlay_inputs,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from ccc_storage_mountd.daemon import MountdService


class AutoCommitWorker:
    """Policy-driven auto-commit, off the hot read path."""

    def __init__(
        self,
        service: MountdService,
        *,
        policy: CommitPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.service = service
        self.policy = policy or CommitPolicy()
        self._clock = clock

    def _child_policy(self, manifest: ChildManifest) -> CommitPolicy:
        # The per-child manifest can force manual-only; thresholds stay shared.
        return replace(self.policy, mode=manifest.commit_mode or "auto")

    def evaluate_child(self, manifest: ChildManifest) -> str:
        paths = self.service.overlay_paths(manifest)
        ensure_active_upper(paths)
        inputs = overlay_inputs(paths.active_upper, now=self._clock())
        return evaluate(self._child_policy(manifest), inputs)

    def _commit(self, manifest: ChildManifest) -> bool:
        try:
            self.service.handle_commit(manifest.id, message="auto-commit")
        except LockHeld:
            return False
        return True

    def tick(self) -> dict[str, Any]:
        """Evaluate every managed child once and commit those that trigger."""
        self.service.reload_registry()
        decisions: dict[str, str] = {}
        committed: list[str] = []
        skipped: list[dict[str, str]] = []
        for manifest in sorted(self.service.children.values(), key=lambda m: m.id):
            decision = self.evaluate_child(manifest)
            decisions[manifest.id] = decision
            if decision != TRIGGER:
                continue
            if self._commit(manifest):
                committed.append(manifest.id)
            else:
                skipped.append({"id": manifest.id, "reason": "locked"})
        return {"decisions": decisions, "committed": committed, "skipped": skipped}

    def poke(self, selector: str) -> dict[str, Any]:
        """Deterministic test/control hook: evaluate one child immediately."""
        manifest = self.service._find(selector)
        decision = self.evaluate_child(manifest)
        committed = False
        skipped_reason = ""
        if decision == TRIGGER:
            if self._commit(manifest):
                committed = True
            else:
                skipped_reason = "locked"
        result: dict[str, Any] = {
            "id": manifest.id,
            "decision": decision,
            "committed": committed,
        }
        if skipped_reason:
            result["skipped"] = skipped_reason
        return result
