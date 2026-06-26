"""Mount-closure computation for external HPC packsets."""

from __future__ import annotations

from ccc_layered_core.manifest import ChildManifest
from ccc_layered_core.resolve import BoundaryRegistry
from ccc_layered_pack.bundle import MountGraph, MountGraphNode


def compute_mount_closure(
    manifests: list[ChildManifest] | tuple[ChildManifest, ...],
    *,
    root_id: str,
    include_child_ids: set[str],
) -> MountGraph:
    """Compute a minimal root+selected-children mount graph.

    Immediate child boundaries not selected are represented as explicit excluded
    stubs so external HPC jobs fail fast instead of silently seeing ENOENT.
    """
    by_id = {manifest.id: manifest for manifest in manifests}
    if root_id not in by_id:
        raise KeyError(root_id)
    registry = BoundaryRegistry.from_manifests(manifests)
    included = [MountGraphNode(child_id=root_id, path=".")]
    excluded: list[MountGraphNode] = []
    for boundary in registry.boundaries_of(root_id):
        if boundary.child_id in include_child_ids:
            included.append(MountGraphNode(child_id=boundary.child_id, path=boundary.path))
        else:
            excluded.append(
                MountGraphNode(
                    child_id=boundary.child_id,
                    path=boundary.path,
                    reason="child boundary not included in this HPC closure",
                )
            )
    return MountGraph(root=root_id, included=tuple(included), excluded=tuple(excluded))
