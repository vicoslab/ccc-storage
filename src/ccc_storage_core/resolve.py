"""Path-to-child-boundary helpers for hierarchical manifests.

The longest-prefix boundary match here is the single source of truth shared by
the dispatcher, the CLI, overlay write-routing, commit-owner selection, and HPC
export. Computing the owning boundary identically everywhere is what keeps
parent and child packs independent (D-13/D-14/D-15).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from ccc_storage_core.manifest import ChildBoundary, ChildManifest


@dataclass(frozen=True)
class BoundaryMatch:
    boundary: ChildBoundary | None
    relative_path: str


@dataclass(frozen=True)
class OwningBoundary:
    """The manifest that owns a path: a child boundary, or the parent itself."""

    owner_id: str
    boundary: ChildBoundary | None
    relative_path: str
    inner_path: str

    @property
    def is_parent(self) -> bool:
        """True when no child boundary matched and the parent owns the path."""
        return self.boundary is None


@dataclass(frozen=True)
class ParentRef:
    parent_id: str
    parent_path: str


def _normalize(rel_path: str) -> str:
    return str(PurePosixPath(rel_path)).lstrip("/")


def _longest_prefix(boundaries: Iterable[ChildBoundary], clean: str) -> ChildBoundary | None:
    best: ChildBoundary | None = None
    best_len = -1
    for boundary in boundaries:
        boundary_path = boundary.path.strip("/")
        if not boundary_path:
            continue
        if clean == boundary_path or clean.startswith(boundary_path + "/"):
            if len(boundary_path) > best_len:
                best = boundary
                best_len = len(boundary_path)
    return best


def nearest_boundary(manifest: ChildManifest, rel_path: str) -> BoundaryMatch:
    """Return the longest child-boundary prefix matching *rel_path*."""
    clean = _normalize(rel_path)
    return BoundaryMatch(
        boundary=_longest_prefix(manifest.child_boundaries, clean),
        relative_path=clean,
    )


def resolve_owner_path(
    boundaries: Iterable[ChildBoundary],
    rel_path: str,
    *,
    parent_id: str,
) -> OwningBoundary:
    """Resolve *rel_path* to the nearest owning boundary (longest-prefix match).

    Paths outside every boundary are owned by *parent_id*; paths under a child
    boundary are owned by that child, with ``inner_path`` relative to the
    boundary root.
    """
    clean = _normalize(rel_path)
    best = _longest_prefix(boundaries, clean)
    if best is None:
        return OwningBoundary(
            owner_id=parent_id, boundary=None, relative_path=clean, inner_path=clean
        )
    boundary_path = best.path.strip("/")
    inner = clean[len(boundary_path) :].lstrip("/")
    return OwningBoundary(
        owner_id=best.child_id,
        boundary=best,
        relative_path=clean,
        inner_path=inner,
    )


class BoundaryRegistry:
    """Bidirectional parent<->child boundary lookup built from manifests.

    Supports the three directions the design calls for: ``parent -> child
    boundaries`` (:meth:`boundaries_of`), ``child -> parent/path``
    (:meth:`parent_of`), and ``absolute path -> owning boundary``
    (:meth:`resolve_owner`).
    """

    def __init__(self) -> None:
        self._boundaries: dict[str, tuple[ChildBoundary, ...]] = {}
        self._parent_of: dict[str, ParentRef] = {}
        self._manifests: dict[str, ChildManifest] = {}

    @classmethod
    def from_manifests(cls, manifests: Iterable[ChildManifest]) -> BoundaryRegistry:
        registry = cls()
        for manifest in manifests:
            registry.add(manifest)
        return registry

    def add(self, manifest: ChildManifest) -> None:
        self._manifests[manifest.id] = manifest
        if manifest.child_boundaries:
            self._boundaries[manifest.id] = manifest.child_boundaries
            for boundary in manifest.child_boundaries:
                self._parent_of[boundary.child_id] = ParentRef(
                    parent_id=manifest.id, parent_path=boundary.path.strip("/")
                )
        # A child manifest may also self-declare its parent ref.
        if manifest.parent_id:
            self._parent_of.setdefault(
                manifest.id,
                ParentRef(
                    parent_id=manifest.parent_id,
                    parent_path=manifest.parent_path.strip("/"),
                ),
            )

    def boundaries_of(self, parent_id: str) -> tuple[ChildBoundary, ...]:
        return self._boundaries.get(parent_id, ())

    def child_ids(self, parent_id: str) -> tuple[str, ...]:
        return tuple(boundary.child_id for boundary in self.boundaries_of(parent_id))

    def parent_of(self, child_id: str) -> ParentRef | None:
        return self._parent_of.get(child_id)

    def resolve_owner(self, parent_id: str, rel_path: str) -> OwningBoundary:
        return resolve_owner_path(self.boundaries_of(parent_id), rel_path, parent_id=parent_id)
