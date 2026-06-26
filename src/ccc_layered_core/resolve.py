"""Path-to-child-boundary helpers for hierarchical manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ccc_layered_core.manifest import ChildBoundary, ChildManifest


@dataclass(frozen=True)
class BoundaryMatch:
    boundary: ChildBoundary | None
    relative_path: str


def nearest_boundary(manifest: ChildManifest, rel_path: str) -> BoundaryMatch:
    """Return the longest child-boundary prefix matching *rel_path*."""
    clean = str(PurePosixPath(rel_path)).lstrip("/")
    best: ChildBoundary | None = None
    for boundary in manifest.child_boundaries:
        boundary_path = boundary.path.strip("/")
        if clean == boundary_path or clean.startswith(boundary_path + "/"):
            if best is None or len(boundary_path) > len(best.path.strip("/")):
                best = boundary
    return BoundaryMatch(boundary=best, relative_path=clean)
