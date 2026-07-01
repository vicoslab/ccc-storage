"""Read-only child mount lifecycle for mountd."""

from __future__ import annotations

import shutil
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccc_storage_core.locks import LockHeld, NFSLock
from ccc_storage_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    normalize_write_policy,
)
from ccc_storage_core.names import safe_namespace_name
from ccc_storage_mountd.overlay import (
    DirtyMirror,
    LocalOverlayPaths,
    OverlayPaths,
    dirty_mirror_paths,
    dirty_stats,
    ensure_active_upper,
    ensure_local_upper,
    latest_dirty_mirror,
    local_overlay_paths,
    publish_logical_mirror,
)
from ccc_storage_mountd.ownership import Ownership
from ccc_storage_pack.reader import (
    MountHandle,
    mount_bind_ro,
    mount_dirs_and_packs_ro,
    mount_layered_rw,
    mount_layered_rw_kernel_overlay,
    mount_stack_ro,
)


class ChildMountError(RuntimeError):
    """Raised for child mount lifecycle failures."""


@dataclass
class MountRecord:
    manifest_id: str
    mountpoint: Path
    handle: MountHandle
    mode: str = "ro"
    write_policy: str = WRITE_POLICY_SHARED_NFS
    refcount: int = 1
    last_used: float = 0.0
    writer_lock: NFSLock | None = None
    local_paths: LocalOverlayPaths | None = None
    base_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "mountpoint": str(self.mountpoint),
            "mounted": self.handle.mounted,
            "mode": self.mode,
            "write_policy": self.write_policy,
            "refcount": self.refcount,
        }


def _safe_name(value: str) -> str:
    return safe_namespace_name(value)


class ChildMountManager:
    """Owns node-local read-only child mounts.

    The manager deliberately does not serve file bytes; it only calls the pack
    reader once per child and tracks refcounts for explicit mount/umount calls.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        prefer_kernel: bool = False,
        nfs_root: str | Path | None = None,
        local_overlay_root: str | Path | None = None,
        node_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        ownership: Ownership | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.prefer_kernel = prefer_kernel
        self.nfs_root = Path(nfs_root) if nfs_root is not None else None
        self.local_overlay_root = (
            Path(local_overlay_root)
            if local_overlay_root is not None
            else self.run_dir / "local-overlays"
        )
        self.node_id = node_id or socket.gethostname()
        self.ownership = ownership or Ownership()
        self.mounts_dir = self.run_dir / "mounts"
        self.mounts_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._records: dict[str, MountRecord] = {}
        self._publish_lock = threading.RLock()
        self._mount_lock = threading.RLock()

    def mount(self, manifest: ChildManifest) -> MountRecord:
        return self.mount_at(
            manifest,
            self.mounts_dir / _safe_name(manifest.id),
            require_existing=False,
        )

    def mount_rw(self, manifest: ChildManifest) -> MountRecord:
        return self.mount_rw_at(
            manifest,
            self.mounts_dir / _safe_name(manifest.id),
            require_existing=False,
        )

    def _reuse_existing(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        mode: str,
        increment_ref: bool = True,
    ) -> MountRecord | None:
        existing = self._records.get(manifest.id)
        if not (existing and existing.handle.mounted):
            return None
        requested = Path(mountpoint)
        if existing.mountpoint != requested:
            raise ChildMountError(
                f"manifest {manifest.id} is already mounted at "
                f"{existing.mountpoint}, not requested mountpoint {requested}"
            )
        if existing.mode != mode:
            raise ChildMountError(
                f"manifest {manifest.id} is already mounted in {existing.mode} mode, "
                f"not requested {mode} mode"
            )
        if existing.write_policy != manifest.write_policy:
            raise ChildMountError(
                f"manifest {manifest.id} is already mounted with "
                f"{existing.write_policy} policy, not manifest policy {manifest.write_policy}"
            )
        if increment_ref:
            existing.refcount += 1
        existing.last_used = self._clock()
        return existing

    def mount_at(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool = True,
        increment_ref: bool = True,
    ) -> MountRecord:
        with self._mount_lock:
            return self._mount_at_unlocked(
                manifest,
                mountpoint,
                require_existing=require_existing,
                increment_ref=increment_ref,
            )

    def _mount_at_unlocked(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool = True,
        increment_ref: bool = True,
    ) -> MountRecord:
        """Mount *manifest* at an explicit mountpoint.

        Nested pack boundaries use a mountpoint inside the already-mounted parent
        view. The boundary directory must already exist there; this prevents a
        missing parent stub from being silently created outside the root pack.
        """
        existing = self._reuse_existing(
            manifest,
            mountpoint,
            mode="ro",
            increment_ref=increment_ref,
        )
        if existing is not None:
            return existing
        if not manifest.pack_stack.lowers:
            raise ChildMountError(f"manifest {manifest.id} has no pack lowers")
        mountpoint = Path(mountpoint)
        if require_existing and not mountpoint.is_dir():
            raise ChildMountError(
                f"mountpoint for {manifest.id} is not an existing directory: {mountpoint}"
            )
        if not require_existing:
            mountpoint.mkdir(parents=True, exist_ok=True)
        handle = mount_stack_ro(
            manifest.pack_stack.lowers,
            mountpoint,
            prefer_kernel=self.prefer_kernel,
        )
        record = MountRecord(
            manifest_id=manifest.id,
            mountpoint=mountpoint,
            handle=handle,
            mode="ro",
            write_policy=manifest.write_policy,
            last_used=self._clock(),
        )
        self._records[manifest.id] = record
        return record

    def mount_rw_at(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool = True,
        prepare_mountpoint: bool = True,
        increment_ref: bool = True,
    ) -> MountRecord:
        with self._mount_lock:
            return self._mount_rw_at_unlocked(
                manifest,
                mountpoint,
                require_existing=require_existing,
                prepare_mountpoint=prepare_mountpoint,
                increment_ref=increment_ref,
            )

    def _mount_rw_at_unlocked(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool = True,
        prepare_mountpoint: bool = True,
        increment_ref: bool = True,
    ) -> MountRecord:
        """Mount *manifest* as a writable child view using its write policy."""
        existing = self._reuse_existing(
            manifest,
            mountpoint,
            mode="rw",
            increment_ref=increment_ref,
        )
        if existing is not None:
            return existing
        policy = normalize_write_policy(manifest.write_policy)
        if policy == WRITE_POLICY_SHARED_NFS:
            return self._mount_rw_shared_nfs(
                manifest,
                mountpoint,
                require_existing=require_existing,
                prepare_mountpoint=prepare_mountpoint,
            )
        if policy == WRITE_POLICY_LOCAL_SSD_ASYNC:
            return self._mount_rw_local_ssd_async(
                manifest,
                mountpoint,
                require_existing=require_existing,
                prepare_mountpoint=prepare_mountpoint,
            )
        raise ChildMountError(f"unsupported write policy for {manifest.id}: {policy}")

    def _mount_rw_shared_nfs(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool,
        prepare_mountpoint: bool,
    ) -> MountRecord:
        mountpoint = Path(mountpoint)
        if require_existing and not mountpoint.is_dir():
            raise ChildMountError(
                f"mountpoint for {manifest.id} is not an existing directory: {mountpoint}"
            )
        if not require_existing and prepare_mountpoint:
            mountpoint.mkdir(parents=True, exist_ok=True)
            self.ownership.apply(mountpoint)
        active_upper = Path(manifest.overlay.active_upper)
        if not manifest.overlay.active_upper:
            raise ChildMountError(f"manifest {manifest.id} has no active overlay upper")
        overlay_root = active_upper.parent
        overlay_paths = OverlayPaths(
            root=overlay_root,
            active_upper=active_upper,
            sealed_dir=overlay_root / "sealed",
        )
        ensure_active_upper(overlay_paths, self.ownership)
        handle = mount_layered_rw(
            manifest.pack_stack.lowers,
            overlay_paths,
            mountpoint,
            prefer_kernel=self.prefer_kernel,
            stack_root=self.run_dir / "stacks" / _safe_name(manifest.id),
            prepare_mountpoint=prepare_mountpoint,
        )
        record = MountRecord(
            manifest_id=manifest.id,
            mountpoint=mountpoint,
            handle=handle,
            mode="rw",
            write_policy=WRITE_POLICY_SHARED_NFS,
            last_used=self._clock(),
            base_generation=manifest.generation,
        )
        self._records[manifest.id] = record
        return record

    def _writer_lock_path(self, manifest_id: str) -> Path:
        if self.nfs_root is None:
            raise ChildMountError("local-ssd-async requires ChildMountManager nfs_root")
        return self.nfs_root / "locks" / f"{_safe_name(manifest_id)}.local-writer.lock"

    def writer_lock_path(self, manifest_id: str) -> Path:
        return self._writer_lock_path(manifest_id)

    def mount_lock(self):
        return self._mount_lock

    def _hydrate_local_upper_from_latest_mirror(
        self,
        manifest: ChildManifest,
        local_paths: LocalOverlayPaths,
    ) -> None:
        upper_has_content = (
            local_paths.active_upper.exists() and any(local_paths.active_upper.iterdir())
        )
        if self.nfs_root is None or upper_has_content:
            return
        latest = latest_dirty_mirror(self.nfs_root, manifest.id)
        if latest is None:
            return
        if local_paths.active_upper.exists():
            shutil.rmtree(local_paths.active_upper)
        shutil.copytree(latest.path, local_paths.active_upper, symlinks=True)
        self.ownership.apply_tree(local_paths.active_upper)

    def _mount_published_mirror_ro(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        prepare_mountpoint: bool,
    ) -> MountRecord:
        if self.nfs_root is None:
            raise ChildMountError("local-ssd-async requires ChildMountManager nfs_root")
        latest = latest_dirty_mirror(self.nfs_root, manifest.id)
        if latest is None:
            if manifest.pack_stack.lowers:
                return self._mount_at_unlocked(
                    manifest,
                    mountpoint,
                    require_existing=False,
                    increment_ref=False,
                )
            raise ChildMountError(
                f"local writer lock is held for {manifest.id} and no published mirror exists"
            )
        mnt = Path(mountpoint)
        if prepare_mountpoint:
            mnt.mkdir(parents=True, exist_ok=True)
            self.ownership.apply(mnt)
        handle: MountHandle
        if manifest.pack_stack.lowers:
            handle = mount_dirs_and_packs_ro(
                (latest.path,),
                manifest.pack_stack.lowers,
                mnt,
                prefer_kernel=False,
                stack_root=self.run_dir / "stacks" / f"{_safe_name(manifest.id)}.mirror",
                prepare_mountpoint=prepare_mountpoint,
            )
        else:
            handle = mount_bind_ro(latest.path, mnt, prepare_mountpoint=prepare_mountpoint)
        record = MountRecord(
            manifest_id=manifest.id,
            mountpoint=mnt,
            handle=handle,
            mode="ro",
            write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
            last_used=self._clock(),
            base_generation=manifest.generation,
        )
        self._records[manifest.id] = record
        return record

    def _mount_rw_local_ssd_async(
        self,
        manifest: ChildManifest,
        mountpoint: str | Path,
        *,
        require_existing: bool,
        prepare_mountpoint: bool,
    ) -> MountRecord:
        if self.nfs_root is None:
            raise ChildMountError("local-ssd-async requires ChildMountManager nfs_root")
        mountpoint = Path(mountpoint)
        if require_existing and not mountpoint.is_dir():
            raise ChildMountError(
                f"mountpoint for {manifest.id} is not an existing directory: {mountpoint}"
            )
        lock = NFSLock(self._writer_lock_path(manifest.id), op="local-writer")
        try:
            lock.acquire()
        except LockHeld:
            return self._mount_published_mirror_ro(
                manifest,
                mountpoint,
                prepare_mountpoint=prepare_mountpoint,
            )
        local_paths = local_overlay_paths(self.local_overlay_root, manifest.id)
        local_paths.root.mkdir(parents=True, exist_ok=True)
        self.ownership.apply(local_paths.root)
        ensure_local_upper(local_paths, self.ownership)
        self._hydrate_local_upper_from_latest_mirror(manifest, local_paths)
        try:
            handle = mount_layered_rw_kernel_overlay(
                manifest.pack_stack.lowers,
                local_paths,
                mountpoint,
                prefer_kernel=False,
                stack_root=self.run_dir / "stacks" / f"{_safe_name(manifest.id)}.local",
                prepare_mountpoint=prepare_mountpoint,
            )
        except Exception:
            lock.release()
            raise
        record = MountRecord(
            manifest_id=manifest.id,
            mountpoint=mountpoint,
            handle=handle,
            mode="rw",
            write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
            last_used=self._clock(),
            writer_lock=lock,
            local_paths=local_paths,
            base_generation=manifest.generation,
        )
        self._records[manifest.id] = record
        return record

    def _publish_record(self, record: MountRecord) -> DirtyMirror | None:
        with self._publish_lock:
            if (
                record.write_policy != WRITE_POLICY_LOCAL_SSD_ASYNC
                or record.mode != "rw"
                or record.writer_lock is None
                or self.nfs_root is None
            ):
                return None
            try:
                record.writer_lock.heartbeat()
            except Exception:
                pass
            if record.local_paths is None or not record.local_paths.active_upper.is_dir():
                return None
            stats = dirty_stats(record.local_paths.active_upper)
            if not stats.dirty:
                return None
            return publish_logical_mirror(
                record.local_paths.active_upper,
                dirty_mirror_paths(self.nfs_root, record.manifest_id),
                child_id=record.manifest_id,
                node_id=self.node_id,
                base_generation=record.base_generation,
                ownership=self.ownership,
            )

    def publish_dirty(self, manifest_id: str) -> DirtyMirror | None:
        record = self._records.get(manifest_id)
        if record is None:
            return None
        return self._publish_record(record)

    def publish_all_dirty(self) -> list[DirtyMirror]:
        published: list[DirtyMirror] = []
        for record in list(self._records.values()):
            mirror = self._publish_record(record)
            if mirror is not None:
                published.append(mirror)
        return published

    def cleanup_local_child(self, manifest_id: str) -> None:
        """Remove node-local SSD dirty state for a child after durable commit/abort."""

        paths = local_overlay_paths(self.local_overlay_root, manifest_id)
        shutil.rmtree(paths.root, ignore_errors=True)

    def unmount(self, manifest_id: str) -> dict[str, Any]:
        record = self._records.get(manifest_id)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        record.refcount -= 1
        if record.refcount <= 0:
            self._publish_record(record)
            record.handle.unmount()
            if record.writer_lock is not None:
                record.writer_lock.release()
            self._records.pop(manifest_id, None)
            return {"mounted": False, "refcount": 0, "mountpoint": str(record.mountpoint)}
        return record.to_dict()

    def force_unmount(self, manifest_id: str) -> dict[str, Any]:
        """Unmount a child regardless of refcount and forget its record."""

        record = self._records.pop(manifest_id, None)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        self._publish_record(record)
        record.handle.unmount()
        if record.writer_lock is not None:
            record.writer_lock.release()
        return {"mounted": False, "refcount": 0, "mountpoint": str(record.mountpoint)}

    def release(self, manifest_id: str) -> dict[str, Any]:
        """Drop one open handle without unmounting.

        Unlike :meth:`unmount`, this never tears the mount down: a child whose
        refcount reaches zero lingers (lazily mounted) until the idle reaper
        decides it has been unused for long enough. This is the lazy-mount /
        idle-unmount model the managed parent relies on.
        """
        record = self._records.get(manifest_id)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        if record.refcount > 0:
            record.refcount -= 1
        record.last_used = self._clock()
        return record.to_dict()

    def idle_unmount_expired(self, ttl: float, *, now: float | None = None) -> list[str]:
        """Unmount children idle (refcount 0) for at least *ttl* seconds.

        Returns the ids actually unmounted. A child with any open handle
        (refcount > 0) is never unmounted, regardless of age.
        """
        current = self._clock() if now is None else now
        expired: list[str] = []
        for manifest_id in list(self._records):
            record = self._records[manifest_id]
            if record.refcount > 0:
                continue
            if current - record.last_used >= ttl:
                self._publish_record(record)
                record.handle.unmount()
                if record.writer_lock is not None:
                    record.writer_lock.release()
                self._records.pop(manifest_id, None)
                expired.append(manifest_id)
        return expired

    def status(self, manifest: ChildManifest) -> dict[str, Any]:
        record = self._records.get(manifest.id)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        return record.to_dict()

    def active_ids(self) -> list[str]:
        """Ids of all currently-mounted children (including idle-but-lingering)."""
        return [mid for mid, record in self._records.items() if record.handle.mounted]

    def active_count(self) -> int:
        return len(self.active_ids())

    def stop_all(self) -> None:
        for manifest_id in reversed(list(self._records)):
            record = self._records.pop(manifest_id)
            self._publish_record(record)
            record.handle.unmount()
            if record.writer_lock is not None:
                record.writer_lock.release()


class NestedMountManager:
    """Lazy nested-submount bookkeeping for one managed-parent view (Option A).

    The parent pack is mounted once; each child boundary is a *nested* submount
    that mounts lazily on first access and idle-unmounts independently while the
    parent view stays mounted. This keeps the mount table bounded for parents
    with many children (e.g. 100 conda envs) without mountd ever entering child
    file I/O — the kernel/squashfuse submount serves the bytes.
    """

    def __init__(self, mounts: ChildMountManager, *, parent_id: str) -> None:
        self._mounts = mounts
        self._parent_id = parent_id
        self._parent_mounted = False

    def mount_parent(self, manifest: ChildManifest) -> MountRecord:
        record = self._mounts.mount(manifest)
        self._parent_mounted = True
        return record

    @property
    def parent_mounted(self) -> bool:
        return self._parent_mounted

    def access_child(self, manifest: ChildManifest) -> MountRecord:
        """Lazily mount the accessed child submount."""
        return self._mounts.mount(manifest)

    def release_child(self, child_id: str) -> dict[str, Any]:
        return self._mounts.release(child_id)

    def idle_reap(self, ttl: float, *, now: float | None = None) -> list[str]:
        """Idle-unmount expired child submounts; the parent is never reaped here."""
        return [
            mid for mid in self._mounts.idle_unmount_expired(ttl, now=now) if mid != self._parent_id
        ]

    def is_child_mounted(self, child_id: str) -> bool:
        return child_id in self._mounts.active_ids()

    def active_ids(self) -> list[str]:
        return [mid for mid in self._mounts.active_ids() if mid != self._parent_id]

    def active_child_count(self) -> int:
        """Active nested submount count, excluding the parent view itself."""
        return len(self.active_ids())
