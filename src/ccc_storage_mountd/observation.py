"""Service-level model for marker-driven observation roots."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ccc_storage_core.manifest import (
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    OverlayInfo,
    PackStack,
    dump_atomic,
    load_manifest,
    normalize_write_policy,
)
from ccc_storage_core.observe import immediate_child_boundaries, resolve_observed_child
from ccc_storage_mountd.childmount import ChildMountManager
from ccc_storage_mountd.managed_parent import ChildExistsError, ChildNotFoundError
from ccc_storage_mountd.overlay import OverlayPaths, dirty_stats, ensure_active_upper
from ccc_storage_mountd.ownership import Ownership
from ccc_storage_pack.builder import safe_pack_name


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
        *,
        default_write_policy: str = WRITE_POLICY_SHARED_NFS,
        ownership: Ownership | None = None,
        prepare_manifest: Callable[[ChildManifest], ChildManifest] | None = None,
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.source_root = Path(source_root)
        self.mounts = mounts
        self.default_write_policy = normalize_write_policy(default_write_policy)
        self.ownership = ownership or Ownership()
        self.prepare_manifest = prepare_manifest or (lambda manifest: manifest)
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
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.ownership.apply(self.registry_dir)
        (self.source_root / boundary_path).mkdir(parents=True, exist_ok=True)
        self.ownership.apply(self.source_root / boundary_path)
        overlay_paths = OverlayPaths.for_child(
            self.overlays_root,
            self.child_id(boundary_path),
        )
        ensure_active_upper(overlay_paths, self.ownership)
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
                active_upper=str(overlay_paths.active_upper),
                overlay_generation=0,
            ),
            write_policy=observed.observation_root.write_policy or self.default_write_policy,
        )
        dump_atomic(manifest_path, manifest)
        self.ownership.apply(manifest_path)
        return self._status(manifest)

    def access_child(self, rel_path: str) -> dict[str, Any]:
        observed = resolve_observed_child(self.source_root, rel_path)
        if observed is None:
            raise ChildNotFoundError(f"observed child not found for path: {rel_path}")
        manifest_path = self.manifest_path_for_boundary(observed.boundary_path)
        if not manifest_path.exists():
            raise ChildNotFoundError(f"observed child is not registered: {observed.boundary_path}")
        manifest = self.prepare_manifest(load_manifest(manifest_path))
        self.mounts.mount_rw(manifest)
        return self._status(manifest)

    def access_child_at(self, rel_path: str, mountpoint: str | Path) -> dict[str, Any]:
        observed = resolve_observed_child(self.source_root, rel_path)
        if observed is None:
            raise ChildNotFoundError(f"observed child not found for path: {rel_path}")
        manifest_path = self.manifest_path_for_boundary(observed.boundary_path)
        if not manifest_path.exists():
            raise ChildNotFoundError(f"observed child is not registered: {observed.boundary_path}")
        manifest = self.prepare_manifest(load_manifest(manifest_path))
        self.mounts.mount_rw_at(
            manifest,
            mountpoint,
            require_existing=False,
            prepare_mountpoint=False,
            increment_ref=False,
        )
        return self._status(manifest)


    def _exact_observed_boundary(self, rel_path: str, *, allow_missing: bool = False):
        observed = resolve_observed_child(
            self.source_root,
            rel_path,
            allow_missing=allow_missing,
        )
        normalized = rel_path.strip("/")
        if observed is None or observed.boundary_path != normalized:
            raise ObservationError(f"path is not an observed child boundary: {rel_path}")
        return observed

    def _assert_mutable_generation0(self, manifest: ChildManifest) -> OverlayPaths:
        if self.mounts.status(manifest)["mounted"]:
            raise ObservationError(f"observed child is mounted: {manifest.parent_path}")
        if manifest.generation != 0 or manifest.pack_stack.lowers:
            raise ObservationError(
                "only clean generation-0 observed children can be renamed or removed"
            )
        overlay_paths = OverlayPaths.for_child(self.overlays_root, manifest.id)
        stats = dirty_stats(overlay_paths.active_upper)
        if stats.dirty:
            raise ObservationError(f"observed child overlay is dirty: {manifest.parent_path}")
        return overlay_paths

    def rmdir_child(self, rel_path: str) -> dict[str, Any]:
        observed = self._exact_observed_boundary(rel_path)
        manifest_path = self.manifest_path_for_boundary(observed.boundary_path)
        manifest = load_manifest(manifest_path) if manifest_path.exists() else None
        overlay_paths = self._assert_mutable_generation0(manifest) if manifest else None
        child_path = self.source_root / observed.boundary_path
        try:
            child_path.rmdir()
        except OSError as exc:
            raise ObservationError(f"cannot remove observed child: {exc}") from exc
        if manifest_path.exists():
            manifest_path.unlink()
        if overlay_paths is not None:
            shutil.rmtree(overlay_paths.root, ignore_errors=True)
        return {"removed": observed.boundary_path}

    def rename_child(self, old_path: str, new_path: str) -> dict[str, Any]:
        old = self._exact_observed_boundary(old_path)
        new = self._exact_observed_boundary(new_path, allow_missing=True)
        if old.observation_root.relative_path != new.observation_root.relative_path:
            raise ObservationError("cannot rename observed child across observation roots")
        old_manifest_path = self.manifest_path_for_boundary(old.boundary_path)
        if not old_manifest_path.exists():
            raise ChildNotFoundError(f"observed child is not registered: {old.boundary_path}")
        new_manifest_path = self.manifest_path_for_boundary(new.boundary_path)
        if new_manifest_path.exists() or (self.source_root / new.boundary_path).exists():
            raise ChildExistsError(f"observed child already exists: {new.boundary_path}")
        manifest = load_manifest(old_manifest_path)
        old_overlay_paths = self._assert_mutable_generation0(manifest)
        (self.source_root / old.boundary_path).rename(self.source_root / new.boundary_path)
        new_id = self.child_id(new.boundary_path)
        new_overlay_paths = OverlayPaths.for_child(self.overlays_root, new_id)
        updated = replace(
            manifest,
            id=new_id,
            name=new.child_name,
            parent_id=f"observe-root:{new.observation_root.relative_path}",
            parent_path=new.boundary_path,
            overlay=OverlayInfo(
                mode="shared-overlay",
                active_upper=str(new_overlay_paths.active_upper),
                overlay_generation=manifest.overlay.overlay_generation,
            ),
        )
        if old_overlay_paths.root.exists():
            shutil.rmtree(old_overlay_paths.root, ignore_errors=True)
        dump_atomic(new_manifest_path, updated)
        self.ownership.apply(new_manifest_path)
        self.ownership.apply(self.source_root / new.boundary_path)
        old_manifest_path.unlink()
        return self._status(updated)

    def _status(self, manifest: ChildManifest) -> dict[str, Any]:
        mount_status = self.mounts.status(manifest)
        overlay_paths = OverlayPaths.for_child(self.overlays_root, manifest.id)
        ensure_active_upper(overlay_paths, self.ownership)
        stats = dirty_stats(overlay_paths.active_upper)
        state = "dirty" if stats.dirty else manifest.state
        return {
            "id": manifest.id,
            "safe_name": safe_pack_name(manifest.parent_path),
            "name": manifest.name,
            "type": manifest.type,
            "parent_path": manifest.parent_path,
            "generation": manifest.generation,
            "write_policy": manifest.write_policy,
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
