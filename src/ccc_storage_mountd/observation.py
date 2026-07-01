"""Service-level model for marker-driven observation roots."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from ccc_storage_core.observe import (
    ObservationRoot,
    ObservedChild,
    immediate_child_boundaries,
    resolve_observed_child,
)
from ccc_storage_mountd.childmount import ChildMountManager
from ccc_storage_mountd.config import ObservationDirConfig
from ccc_storage_mountd.managed_parent import ChildExistsError, ChildNotFoundError
from ccc_storage_mountd.overlay import OverlayPaths, dirty_stats, ensure_active_upper
from ccc_storage_mountd.ownership import Ownership
from ccc_storage_pack.builder import safe_pack_name


class ObservationError(RuntimeError):
    """Raised when an observe-root request cannot be resolved."""


STATE_SUBDIRS = ("registry", "packs", "overlays", "locks", "events")
ROOT_ID_FILE = "root-id"


@dataclass(frozen=True)
class ObservationStorage:
    """Resolved paths for one observation directory."""

    public_path: Path
    source_root: Path
    state_dir: Path
    state_subdir: str
    root_id: str

    @property
    def registry_dir(self) -> Path:
        return self.state_dir / "registry" / "observe"

    @property
    def packs_root(self) -> Path:
        return self.state_dir / "packs"

    @property
    def overlays_root(self) -> Path:
        return self.state_dir / "overlays"

    @property
    def locks_root(self) -> Path:
        return self.state_dir / "locks"

    @property
    def events_root(self) -> Path:
        return self.state_dir / "events"


def initialize_observation_dir(
    path: str | Path,
    *,
    state_subdir: str = ".ccc-storage",
    ownership: Ownership | None = None,
) -> ObservationStorage:
    """Create and return persistent state for one observation directory."""

    if not state_subdir or "/" in state_subdir or state_subdir in {".", ".."}:
        raise ObservationError("state_subdir must be a directory name")
    owner = ownership or Ownership()
    public_path = Path(path)
    public_path.mkdir(parents=True, exist_ok=True)
    owner.apply(public_path)
    state_dir = public_path / state_subdir
    for name in STATE_SUBDIRS:
        directory = state_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        owner.apply(directory)
    root_id_path = state_dir / ROOT_ID_FILE
    if root_id_path.exists():
        root_id = root_id_path.read_text(encoding="utf-8").strip()
    else:
        root_id = uuid.uuid4().hex
        root_id_path.write_text(root_id + "\n", encoding="utf-8")
        owner.apply(root_id_path)
    if not root_id:
        raise ObservationError(f"empty observation root id: {root_id_path}")
    return ObservationStorage(
        public_path=public_path,
        source_root=public_path,
        state_dir=state_dir,
        state_subdir=state_subdir,
        root_id=root_id,
    )


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
        child_id_prefix: str = "observe",
        marker_required: bool = True,
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.source_root = Path(source_root)
        self.mounts = mounts
        self.default_write_policy = normalize_write_policy(default_write_policy)
        self.ownership = ownership or Ownership()
        self.prepare_manifest = prepare_manifest or (lambda manifest: manifest)
        self.child_id_prefix = child_id_prefix.strip(":") or "observe"
        self.marker_required = marker_required
        self.registry_dir = self.nfs_root / "registry" / "observe"
        self.overlays_root = self.nfs_root / "overlays"

    def manifest_path_for_boundary(self, boundary_path: str) -> Path:
        return self.registry_dir / f"{safe_pack_name(boundary_path)}.toml"

    def child_id(self, boundary_path: str) -> str:
        return f"{self.child_id_prefix}:{boundary_path.strip('/')}"

    def list_boundaries(self) -> dict[str, Any]:
        children = []
        for boundary in self._immediate_child_boundaries():
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

    def _immediate_child_boundaries(self) -> tuple[str, ...]:
        if self.marker_required:
            return immediate_child_boundaries(self.source_root)
        if not self.source_root.is_dir():
            return ()
        return tuple(
            entry.name
            for entry in sorted(self.source_root.iterdir(), key=lambda item: item.name)
            if entry.is_dir() and entry.name != self.nfs_root.name
        )

    def _resolve_child(self, rel_path: str, *, allow_missing: bool = False) -> ObservedChild | None:
        if self.marker_required:
            return resolve_observed_child(self.source_root, rel_path, allow_missing=allow_missing)
        raw = rel_path.strip("/")
        if not raw or raw.startswith("/") or "\x00" in raw:
            return None
        parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            return None
        child_name = parts[0]
        boundary_dir = self.source_root / child_name
        if not allow_missing and not boundary_dir.is_dir():
            return None
        if child_name == self.nfs_root.name:
            return None
        return ObservedChild(
            observation_root=ObservationRoot(path=self.source_root, relative_path=""),
            boundary_path=child_name,
            child_name=child_name,
            inner_path="/".join(parts[1:]),
        )

    def mkdir_child(self, rel_path: str) -> dict[str, Any]:
        observed = self._resolve_child(rel_path, allow_missing=True)
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

    def access_child(self, rel_path: str, *, increment_ref: bool = True) -> dict[str, Any]:
        observed = self._resolve_child(rel_path)
        if observed is None:
            raise ChildNotFoundError(f"observed child not found for path: {rel_path}")
        manifest_path = self.manifest_path_for_boundary(observed.boundary_path)
        if not manifest_path.exists():
            raise ChildNotFoundError(f"observed child is not registered: {observed.boundary_path}")
        manifest = self.prepare_manifest(load_manifest(manifest_path))
        self.mounts.mount_rw(manifest, increment_ref=increment_ref)
        return self._status(manifest)

    def access_child_at(self, rel_path: str, mountpoint: str | Path) -> dict[str, Any]:
        observed = self._resolve_child(rel_path)
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
        if not self.marker_required:
            observed = self._resolve_child(rel_path, allow_missing=allow_missing)
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


@dataclass
class RoutedObservationRoot:
    root: ObservationStorage
    manager: ObservationManager


class ObservationRouter:
    """Route observe operations to the nearest configured observation root."""

    def __init__(
        self,
        mounts: ChildMountManager,
        *,
        default_write_policy: str = WRITE_POLICY_SHARED_NFS,
        ownership: Ownership | None = None,
        prepare_manifest: Callable[[ChildManifest], ChildManifest] | None = None,
    ) -> None:
        self.mounts = mounts
        self.default_write_policy = normalize_write_policy(default_write_policy)
        self.ownership = ownership or Ownership()
        self.prepare_manifest = prepare_manifest or (lambda manifest: manifest)
        self._roots: list[RoutedObservationRoot] = []

    @property
    def roots(self) -> tuple[RoutedObservationRoot, ...]:
        return tuple(self._roots)

    def add_root(self, config: ObservationDirConfig | ObservationStorage) -> RoutedObservationRoot:
        if isinstance(config, ObservationStorage):
            storage = config
        else:
            storage = initialize_observation_dir(
                config.path,
                state_subdir=config.state_subdir,
                ownership=self.ownership,
            )
        public = _resolve_path(storage.public_path)
        for routed in self._roots:
            if _resolve_path(routed.root.public_path) == public:
                return routed
        manager = ObservationManager(
            storage.state_dir,
            storage.source_root,
            self.mounts,
            default_write_policy=self.default_write_policy,
            ownership=self.ownership,
            prepare_manifest=self.prepare_manifest,
            child_id_prefix=f"observe:{storage.root_id}",
            marker_required=False,
        )
        routed = RoutedObservationRoot(root=storage, manager=manager)
        self._roots.append(routed)
        self._roots.sort(key=lambda item: len(_resolve_path(item.root.public_path).parts))
        return routed

    def resolve(self, path: str | Path) -> RoutedObservationRoot:
        selected: RoutedObservationRoot | None = None
        candidate = _resolve_path(path)
        for routed in self._roots:
            root_path = _resolve_path(routed.root.public_path)
            if _path_is_relative_to(candidate, root_path):
                if selected is None or len(root_path.parts) > len(
                    _resolve_path(selected.root.public_path).parts
                ):
                    selected = routed
        if selected is None:
            raise ObservationError(f"path is not under a configured observation dir: {path}")
        return selected

    def rel_to_root(self, path: str | Path, routed: RoutedObservationRoot) -> str:
        candidate = _resolve_path(path)
        root_path = _resolve_path(routed.root.public_path)
        try:
            rel = candidate.relative_to(root_path).as_posix()
        except ValueError as exc:
            raise ObservationError(f"path is not under observation dir: {path}") from exc
        return "" if rel == "." else rel

    def _route_path(self, path: str | Path) -> tuple[RoutedObservationRoot, str]:
        if Path(path).is_absolute():
            routed = self.resolve(path)
            rel = self.rel_to_root(path, routed)
            return routed, rel
        if len(self._roots) == 1:
            return self._roots[0], str(path)
        raise ObservationError(f"relative observe path is ambiguous with multiple roots: {path}")

    def list_boundaries(self) -> dict[str, Any]:
        if len(self._roots) == 1:
            return self._roots[0].manager.list_boundaries()
        return {
            "observation_dirs": [
                {
                    "path": str(routed.root.public_path),
                    "state_dir": str(routed.root.state_dir),
                    "children": routed.manager.list_boundaries()["children"],
                }
                for routed in self._roots
            ]
        }

    def mkdir_child(self, path: str | Path) -> dict[str, Any]:
        routed, rel = self._route_path(path)
        return routed.manager.mkdir_child(rel)

    def access_child(self, path: str | Path, *, increment_ref: bool = True) -> dict[str, Any]:
        routed, rel = self._route_path(path)
        return routed.manager.access_child(rel, increment_ref=increment_ref)

    def access_child_at(self, path: str | Path, mountpoint: str | Path) -> dict[str, Any]:
        routed, rel = self._route_path(path)
        return routed.manager.access_child_at(rel, mountpoint)

    def rmdir_child(self, path: str | Path) -> dict[str, Any]:
        routed, rel = self._route_path(path)
        return routed.manager.rmdir_child(rel)

    def rename_child(self, old_path: str | Path, new_path: str | Path) -> dict[str, Any]:
        old_routed, old_rel = self._route_path(old_path)
        new_routed, new_rel = self._route_path(new_path)
        if old_routed.root.public_path != new_routed.root.public_path:
            raise ObservationError("cannot rename observed child across observation roots")
        return old_routed.manager.rename_child(old_rel, new_rel)


def _resolve_path(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    # Do not use Path.resolve() here.  Observation routing is called from live
    # FUSE callbacks with public paths inside the FUSE mount; resolving/statting
    # those paths can re-enter the same dispatcher and deadlock.  Lexical
    # normalization is sufficient because configured observation roots are
    # absolute deployment paths.
    return Path(os.path.normpath(os.fspath(expanded)))


def _path_is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
