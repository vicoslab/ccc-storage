"""Read-only child mount lifecycle for mountd."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccc_layered_core.manifest import ChildManifest
from ccc_layered_pack.reader import MountHandle, mount_stack_ro


class ChildMountError(RuntimeError):
    """Raised for child mount lifecycle failures."""


@dataclass
class MountRecord:
    manifest_id: str
    mountpoint: Path
    handle: MountHandle
    refcount: int = 1
    last_used: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "mountpoint": str(self.mountpoint),
            "mounted": self.handle.mounted,
            "refcount": self.refcount,
        }


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "child"


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.prefer_kernel = prefer_kernel
        self.mounts_dir = self.run_dir / "mounts"
        self.mounts_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._records: dict[str, MountRecord] = {}

    def mount(self, manifest: ChildManifest) -> MountRecord:
        existing = self._records.get(manifest.id)
        if existing and existing.handle.mounted:
            existing.refcount += 1
            existing.last_used = self._clock()
            return existing
        if not manifest.pack_stack.lowers:
            raise ChildMountError(f"manifest {manifest.id} has no pack lowers")
        mountpoint = self.mounts_dir / _safe_name(manifest.id)
        handle = mount_stack_ro(
            manifest.pack_stack.lowers,
            mountpoint,
            prefer_kernel=self.prefer_kernel,
        )
        record = MountRecord(
            manifest_id=manifest.id,
            mountpoint=mountpoint,
            handle=handle,
            last_used=self._clock(),
        )
        self._records[manifest.id] = record
        return record

    def unmount(self, manifest_id: str) -> dict[str, Any]:
        record = self._records.get(manifest_id)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        record.refcount -= 1
        if record.refcount <= 0:
            record.handle.unmount()
            self._records.pop(manifest_id, None)
            return {"mounted": False, "refcount": 0, "mountpoint": str(record.mountpoint)}
        return record.to_dict()

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
                record.handle.unmount()
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
        for manifest_id in list(self._records):
            record = self._records.pop(manifest_id)
            record.handle.unmount()


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
