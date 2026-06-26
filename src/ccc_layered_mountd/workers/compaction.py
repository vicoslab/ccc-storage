"""Delta-pack compaction planner + a safe build/publish skeleton (D-11).

Trigger (D-11): **>8 delta packs OR total delta bytes >20% of the base**. The
planner is a pure, deterministic function of the manifest's pack stack.

Honest scope note: actually *merging* base+deltas into one consolidated pack
requires a materialized layered view (a mounted union), which is a FUSE/runtime
concern out of reach in headless unit tests. :func:`consolidate` therefore
demands a caller-provided materialized source dir and reuses ``build_pack``;
:func:`publish_consolidation` performs the manifest swap. The ordering is
build → verify → publish → retire, matching the phase-03 RK-9 discipline, so a
crash before publish leaves the existing base+deltas intact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_pack.builder import BuildResult, build_pack
from ccc_layered_pack.verify import verify_pack


class CompactionError(RuntimeError):
    """Raised when a compaction build cannot proceed safely."""


@dataclass(frozen=True)
class CompactionPolicy:
    max_deltas: int = 8
    delta_bytes_ratio: float = 0.20


@dataclass(frozen=True)
class CompactionPlan:
    child_id: str
    base: PackInfo
    deltas: tuple[PackInfo, ...]
    reason: str

    @property
    def delta_count(self) -> int:
        return len(self.deltas)


def plan_compaction(
    manifest: ChildManifest, *, policy: CompactionPolicy | None = None
) -> CompactionPlan | None:
    """Return a :class:`CompactionPlan` when the D-11 trigger fires, else ``None``."""
    policy = policy or CompactionPolicy()
    lowers = manifest.pack_stack.lowers
    if len(lowers) <= 1:
        return None
    base = lowers[0]
    deltas = lowers[1:]
    delta_bytes = sum(pack.size for pack in deltas)
    reasons: list[str] = []
    if len(deltas) > policy.max_deltas:
        reasons.append(f"delta-count>{policy.max_deltas}")
    if base.size > 0 and delta_bytes > base.size * policy.delta_bytes_ratio:
        reasons.append(f"delta-bytes>{int(policy.delta_bytes_ratio * 100)}%-base")
    if not reasons:
        return None
    return CompactionPlan(
        child_id=manifest.id,
        base=base,
        deltas=tuple(deltas),
        reason="+".join(reasons),
    )


def consolidate(
    plan: CompactionPlan,
    source_dir: str | Path,
    out_path: str | Path,
    *,
    build_pack_fn: Callable[..., BuildResult] = build_pack,
) -> PackInfo:
    """Build + verify one consolidated pack from a *materialized* layered tree.

    ``source_dir`` must already be the merged view of base+deltas (produced by a
    mounted union — not synthesized here). The new pack is verified against its
    freshly-computed metadata before it is returned; publishing is a separate
    step so a verify failure never mutates the manifest.
    """
    src = Path(source_dir)
    if not src.is_dir():
        raise CompactionError(f"materialized layered source dir required: {src}")
    result = build_pack_fn(src, out_path)
    verify_pack(out_path, result.pack)
    return result.pack


def publish_consolidation(
    manifest: ChildManifest,
    new_pack: PackInfo,
    *,
    new_generation: int | None = None,
) -> tuple[ChildManifest, tuple[PackInfo, ...]]:
    """Swap the pack stack to a single consolidated pack; return retired packs.

    The previous lowers are returned (not deleted) so the caller can hand them to
    the GC planner, which only retires packs that pass every safety predicate.
    """
    retired = manifest.pack_stack.lowers
    generation = manifest.generation + 1 if new_generation is None else new_generation
    updated = replace(
        manifest,
        generation=generation,
        pack_stack=PackStack(active_revision=f"g{generation}", lowers=(new_pack,)),
    )
    return updated, retired
