"""Conservative retention / garbage-collection planner (deferred-Q7, RK-9).

A retired pack is evictable only when the child has **no** dirty overlay, **no**
active mount, **no** pending commit lock, and is **not** ``pinned``. The planner
is conservative by construction: any blocker keeps *all* candidates. An
``admin_override`` flag can bypass the mount/dirty/lock predicates for forced
maintenance, but a ``pinned`` pack is never evicted (RK-9 data-loss guard).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ccc_storage_core.manifest import ChildManifest, PackInfo


@dataclass(frozen=True)
class GCPlan:
    evictable: tuple[PackInfo, ...]
    blocked: tuple[PackInfo, ...]
    reasons: tuple[str, ...]


def plan_gc(
    manifest: ChildManifest,
    retired: Iterable[PackInfo],
    *,
    active_mount: bool,
    dirty: bool,
    pending_lock: bool,
    admin_override: bool = False,
) -> GCPlan:
    """Return the set of retired packs safe to evict for this child."""
    candidates = tuple(retired)
    reasons: list[str] = []
    # Pinned is never overridable: it is the explicit "keep this hot" guarantee.
    if manifest.pinned:
        reasons.append("pinned")
    if not admin_override:
        if dirty:
            reasons.append("dirty-overlay")
        if active_mount:
            reasons.append("active-mount")
        if pending_lock:
            reasons.append("pending-commit")
    if reasons:
        return GCPlan(evictable=(), blocked=candidates, reasons=tuple(reasons))
    return GCPlan(evictable=candidates, blocked=(), reasons=())
