"""Cold-storage archive eligibility policy helpers."""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from pathlib import Path

from ccc_storage_cold.archive import PACK_STATE_COLD
from ccc_storage_core.manifest import ChildManifest


@dataclass(frozen=True)
class ColdArchiveDecision:
    eligible: bool
    reason: str
    idle_age_seconds: float = 0.0


def hot_packs_present(manifest: ChildManifest) -> bool:
    return all(Path(pack.path).is_file() for pack in manifest.pack_stack.lowers)


def needs_recall(manifest: ChildManifest) -> bool:
    if not manifest.pack_stack.lowers:
        return False
    if manifest.cold_storage.pack_state == PACK_STATE_COLD:
        return True
    return not hot_packs_present(manifest)


def last_access_epoch(manifest: ChildManifest) -> float | None:
    value = manifest.cold_storage.last_accessed_at or manifest.cold_storage.last_recalled_at
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def archive_decision(
    manifest: ChildManifest,
    *,
    dirty: bool,
    mounted: bool,
    idle_seconds: float,
    now: float | None = None,
) -> ColdArchiveDecision:
    """Return whether a clean child should be archived to cold storage now."""
    now = time.time() if now is None else now
    if manifest.pinned:
        return ColdArchiveDecision(False, "pinned")
    if mounted:
        return ColdArchiveDecision(False, "mounted")
    if not manifest.pack_stack.lowers:
        return ColdArchiveDecision(False, "no-packs")
    if manifest.cold_storage.pack_state == PACK_STATE_COLD:
        return ColdArchiveDecision(False, "already-cold")
    if not hot_packs_present(manifest):
        return ColdArchiveDecision(False, "hot-pack-missing")
    if dirty:
        return ColdArchiveDecision(False, "dirty")
    accessed = last_access_epoch(manifest)
    if accessed is None:
        # Safe default for legacy manifests: initialize access metadata first;
        # do not unexpectedly archive old data merely because metadata is absent.
        return ColdArchiveDecision(False, "no-access-metadata")
    idle_age = max(0.0, now - accessed)
    if idle_age < idle_seconds:
        return ColdArchiveDecision(False, "recently-accessed", idle_age)
    return ColdArchiveDecision(True, "eligible", idle_age)
