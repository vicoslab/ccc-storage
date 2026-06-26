"""Service-level managed-parent namespace logic.

The managed parent is the transparency keystone: ``ls <parent>`` shows managed
children from their manifests, ``mkdir <parent>/foo`` creates a managed child,
and ``cd <parent>/foo`` lazily mounts it. This module is the pure, unit-testable
core of that behaviour — a *shallow* control-plane layer only.

It must NOT serve bulk file bytes and must NOT implement a read/write path for
child file content (RK-7). Bytes are served once resolution crosses into a child
mount by the kernel / squashfuse via :class:`ChildMountManager`. The (optional)
pyfuse3 adapter that turns this into a real mounted namespace lives in
``dispatcher_fuse`` and is imported lazily there.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from ccc_layered_core.locks import LockHeld, NFSLock
from ccc_layered_core.manifest import (
    ChildManifest,
    OverlayInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_mountd.childmount import ChildMountManager
from ccc_layered_mountd.overlay import (
    OverlayPaths,
    dirty_stats,
    ensure_active_upper,
)

# Names that are internal bookkeeping, never real children. Hidden from listings
# and refused as child names (RK-8).
_HIDDEN_NAMES = frozenset({".ccc-layered", "ccc-layered"})


class ManagedParentError(RuntimeError):
    """Base class for managed-parent namespace errors."""


class ChildExistsError(ManagedParentError):
    """Raised when creating/renaming onto a name that already exists (EEXIST)."""


class ChildNotFoundError(ManagedParentError):
    """Raised when an operation targets a child that does not exist (ENOENT)."""


class ChildNotEmptyError(ManagedParentError):
    """Raised when rmdir is refused because the child has dirty data (ENOTEMPTY)."""


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "child"


def is_internal_name(name: str) -> bool:
    """True for marker/internal names that must be hidden and never created."""
    return name.startswith(".") or name in _HIDDEN_NAMES


def visible_entries(entries: Iterable[str]) -> list[str]:
    """Filter a directory listing to user-visible names (RK-8).

    Hides internal bookkeeping and boundary-marker files (e.g. ``.ccc-boundary``)
    while leaving normal boundary directory names (``env-a``) visible.
    """
    return sorted(name for name in entries if not is_internal_name(name))


class ManagedParent:
    """Pure namespace logic for one managed parent path.

    Children are stored as TOML manifests under
    ``<nfs_root>/registry/<parent_id>/<name>.toml`` so the existing registry
    scan (``MountdService.reload_registry``) still discovers them. Mutating ops
    (``create``/``rename``) are guarded by NFS-safe ``O_EXCL`` locks (D-6).
    """

    def __init__(
        self,
        nfs_root: str | Path,
        run_dir: str | Path,
        *,
        parent_path: str,
        parent_id: str = "",
        mounts: ChildMountManager | None = None,
        prefer_kernel: bool = False,
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.run_dir = Path(run_dir)
        self.parent_path = parent_path
        self.parent_id = parent_id or PurePosixPath(parent_path).name or "managed"
        self.registry_dir = self.nfs_root / "registry"
        self.overlays_root = self.nfs_root / "overlays"
        self.locks_dir = self.nfs_root / "locks"
        self.mounts = mounts or ChildMountManager(self.run_dir, prefer_kernel=prefer_kernel)

    # -- paths ---------------------------------------------------------------

    @property
    def children_dir(self) -> Path:
        path = self.registry_dir / _safe_name(self.parent_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def manifest_path(self, name: str) -> Path:
        return self.children_dir / f"{_safe_name(name)}.toml"

    def _child_id(self, name: str) -> str:
        return f"{self.parent_id}:{name}"

    def _overlay_paths(self, manifest: ChildManifest) -> OverlayPaths:
        return OverlayPaths.for_child(self.overlays_root, manifest.id)

    def _create_lock(self, name: str) -> NFSLock:
        lock_path = self.locks_dir / f"{_safe_name(self.parent_id)}.{_safe_name(name)}.create.lock"
        return NFSLock(lock_path, op="create-child")

    # -- namespace ops -------------------------------------------------------

    def list_children(self) -> list[str]:
        """Return the visible child names, with markers/internal files hidden."""
        names: list[str] = []
        for path in sorted(self.children_dir.glob("*.toml")):
            if path.name.startswith("."):  # half-written / temp manifests
                continue
            try:
                manifest = load_manifest(path)
            except Exception:
                continue
            if is_internal_name(manifest.name):
                continue
            names.append(manifest.name)
        return sorted(names)

    def create_child(self, name: str) -> dict[str, Any]:
        """Atomically create a fresh generation-0 managed child under a lock.

        Exactly one concurrent caller wins; the rest get :class:`ChildExistsError`.
        """
        self._validate_name(name)
        manifest_path = self.manifest_path(name)
        lock = self._create_lock(name)
        try:
            lock.acquire()
        except LockHeld as exc:
            raise ChildExistsError(
                f"child {name!r} is already being created under {self.parent_path}"
            ) from exc
        try:
            if manifest_path.exists():
                raise ChildExistsError(f"child {name!r} already exists under {self.parent_path}")
            child_id = self._child_id(name)
            overlay_paths = OverlayPaths.for_child(self.overlays_root, child_id)
            ensure_active_upper(overlay_paths)
            manifest = ChildManifest(
                id=child_id,
                name=name,
                type="dataset",
                generation=0,
                state="clean",
                parent_id=self.parent_id,
                parent_path=self.parent_path,
                pack_stack=PackStack(),
                overlay=OverlayInfo(
                    mode="shared-overlay",
                    active_upper=str(overlay_paths.active_upper),
                    overlay_generation=0,
                ),
            )
            dump_atomic(manifest_path, manifest)
        finally:
            lock.release()
        return self._status(manifest)

    def rename_child(self, old_name: str, new_name: str) -> dict[str, Any]:
        """Rename a child, updating the manifest name/parent path atomically."""
        self._validate_name(new_name)
        src = self.manifest_path(old_name)
        if not src.exists():
            raise ChildNotFoundError(f"child {old_name!r} not found under {self.parent_path}")
        dst = self.manifest_path(new_name)
        if dst.exists():
            raise ChildExistsError(f"child {new_name!r} already exists under {self.parent_path}")
        manifest = load_manifest(src)
        renamed = ChildManifest(
            id=manifest.id,
            name=new_name,
            type=manifest.type,
            generation=manifest.generation,
            state=manifest.state,
            schema_version=manifest.schema_version,
            parent_id=self.parent_id,
            parent_path=self.parent_path,
            created_ts=manifest.created_ts,
            pack_stack=manifest.pack_stack,
            overlay=manifest.overlay,
            s3=manifest.s3,
            child_boundaries=manifest.child_boundaries,
        )
        dump_atomic(dst, renamed)  # atomic publish of the new manifest
        src.unlink()  # then drop the old name
        return self._status(renamed)

    def remove_child(self, name: str) -> dict[str, Any]:
        """rmdir policy: only empty, uncommitted (generation-0) children go.

        Committed children (any packs / generation > 0) and children with a
        dirty overlay are refused with a clear, ``ccc-layered``-referencing
        error rather than silently dropped (D-21 spirit).
        """
        src = self.manifest_path(name)
        if not src.exists():
            raise ChildNotFoundError(f"child {name!r} not found under {self.parent_path}")
        manifest = load_manifest(src)
        if manifest.generation > 0 or manifest.pack_stack.lowers:
            raise ManagedParentError(
                f"refusing to rmdir committed child {name!r}: it has committed packs "
                f"(generation {manifest.generation}). Use `ccc-layered` to manage it."
            )
        overlay_paths = self._overlay_paths(manifest)
        stats = dirty_stats(overlay_paths.active_upper)
        if stats.dirty:
            raise ChildNotEmptyError(
                f"refusing to rmdir non-empty child {name!r}: "
                f"{stats.file_count} uncommitted file(s) in the overlay."
            )
        src.unlink()
        shutil.rmtree(overlay_paths.root, ignore_errors=True)
        return {"id": manifest.id, "name": name, "removed": True}

    def access_child(self, name: str) -> dict[str, Any]:
        """Lazily mount a child on access and return its status."""
        manifest = self._load(name)
        self.mounts.mount(manifest)
        return self._status(manifest)

    # -- helpers -------------------------------------------------------------

    def _load(self, name: str) -> ChildManifest:
        path = self.manifest_path(name)
        if not path.exists():
            raise ChildNotFoundError(f"child {name!r} not found under {self.parent_path}")
        return load_manifest(path)

    def _validate_name(self, name: str) -> None:
        if not name or name in (".", ".."):
            raise ManagedParentError(f"invalid child name: {name!r}")
        if "/" in name or "\x00" in name:
            raise ManagedParentError(f"invalid child name: {name!r}")
        if is_internal_name(name):
            raise ManagedParentError(f"reserved/internal child name: {name!r}")

    def _status(self, manifest: ChildManifest) -> dict[str, Any]:
        mount_status = self.mounts.status(manifest)
        overlay_paths = self._overlay_paths(manifest)
        ensure_active_upper(overlay_paths)
        stats = dirty_stats(overlay_paths.active_upper)
        state = "dirty" if stats.dirty else manifest.state
        return {
            "id": manifest.id,
            "name": manifest.name,
            "type": manifest.type,
            "parent_path": self.parent_path,
            "generation": manifest.generation,
            "state": state,
            "mounted": bool(mount_status["mounted"]),
            "mountpoint": mount_status["mountpoint"],
            "refcount": mount_status["refcount"],
            "overlay": {
                "active_upper": str(overlay_paths.active_upper),
                "dirty": stats.dirty,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
            },
        }
