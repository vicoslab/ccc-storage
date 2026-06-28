"""Service-level model for marker-driven observation roots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ccc_layered_core.manifest import (
    ChildManifest,
    OverlayInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_core.observe import immediate_child_boundaries, resolve_observed_child
from ccc_layered_mountd.childmount import ChildMountManager
from ccc_layered_mountd.managed_parent import ChildExistsError, ChildNotFoundError
from ccc_layered_mountd.overlay import OverlayPaths, dirty_stats, ensure_active_upper
from ccc_layered_pack.builder import safe_pack_name


class ObservationError(RuntimeError):
    """Raised when an observe-root request cannot be resolved."""


class ObservationManager:
    """Pure control-plane API for future FUSE observation dispatch.

    It registers manifests for observed child boundaries and delegates lazy
    mounts to :class:`ChildMountManager`; it does not serve file bytes.
    """

    def __init__(
        self,
        nfs_root: str | Path,
        source_root: str | Path,
        mounts: ChildMountManager,
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.source_root = Path(source_root)
        self.mounts = mounts
        self.registry_dir = self.nfs_root / "registry" / "observe"
        self.overlays_root = self.nfs_root / "overlays"

    def manifest_path_for_boundary(self, boundary_path: str) -> Path:
        return self.registry_dir / f"{safe_pack_name(boundary_path)}.toml"

    def child_id(self, boundary_path: str) -> str:
        return f"observe:{boundary_path.strip('/')}"

    def list_boundaries(self) -> dict[str, Any]:
        children = []
        for boundary in immediate_child_boundaries(self.source_root):
            path = self.manifest_path_for_boundary(boundary)
            registered = path.exists()
            status = self._status(load_manifest(path)) if registered else None
            children.append(
                {
                    "id": self.child_id(boundary),
                    "path": boundary,
                    "registered": registered,
                    "generation": status["generation"] if status else 0,
                    "mounted": bool(status["mounted"]) if status else False,
                    "status": status,
                }
            )
        return {"observe_root": str(self.source_root), "children": children}

    def mkdir_child(self, rel_path: str) -> dict[str, Any]:
        observed = resolve_observed_child(self.source_root, rel_path, allow_missing=True)
        if observed is None:
            raise ObservationError(f"path is not under an observation root: {rel_path}")
        boundary_path = observed.boundary_path
        manifest_path = self.manifest_path_for_boundary(boundary_path)
        if manifest_path.exists():
            raise ChildExistsError(f"observed child already registered: {boundary_path}")
        (self.source_root / boundary_path).mkdir(parents=True, exist_ok=True)
        manifest = ChildManifest(
            id=self.child_id(boundary_path),
            name=observed.child_name,
            type="observed-child",
            generation=0,
            parent_id=f"observe-root:{observed.observation_root.relative_path}",
            parent_path=boundary_path,
            pack_stack=PackStack(),
            overlay=OverlayInfo(
                mode="shared-overlay",
                active_upper=str(
                    OverlayPaths.for_child(
                        self.overlays_root,
                        self.child_id(boundary_path),
                    ).active_upper
                ),
                overlay_generation=0,
            ),
        )
        dump_atomic(manifest_path, manifest)
        return self._status(manifest)

    def access_child(self, rel_path: str) -> dict[str, Any]:
        observed = resolve_observed_child(self.source_root, rel_path)
        if observed is None:
            raise ChildNotFoundError(f"observed child not found for path: {rel_path}")
        manifest_path = self.manifest_path_for_boundary(observed.boundary_path)
        if not manifest_path.exists():
            raise ChildNotFoundError(f"observed child is not registered: {observed.boundary_path}")
        manifest = load_manifest(manifest_path)
        self.mounts.mount(manifest)
        return self._status(manifest)

    def _status(self, manifest: ChildManifest) -> dict[str, Any]:
        mount_status = self.mounts.status(manifest)
        overlay_paths = OverlayPaths.for_child(self.overlays_root, manifest.id)
        ensure_active_upper(overlay_paths)
        stats = dirty_stats(overlay_paths.active_upper)
        state = "dirty" if stats.dirty else manifest.state
        return {
            "id": manifest.id,
            "safe_name": safe_pack_name(manifest.parent_path),
            "name": manifest.name,
            "type": manifest.type,
            "parent_path": manifest.parent_path,
            "generation": manifest.generation,
            "state": state,
            "mounted": bool(mount_status["mounted"]),
            "mountpoint": mount_status["mountpoint"],
            "refcount": mount_status["refcount"],
            "packs": [pack.to_dict() for pack in manifest.pack_stack.lowers],
            "overlay": {
                "active_upper": str(overlay_paths.active_upper),
                "dirty": stats.dirty,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
            },
        }
