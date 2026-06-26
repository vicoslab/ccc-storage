"""Read-only child mount lifecycle for mountd."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccc_layered_core.manifest import ChildManifest
from ccc_layered_pack.reader import MountHandle, mount_ro


class ChildMountError(RuntimeError):
    """Raised for child mount lifecycle failures."""


@dataclass
class MountRecord:
    manifest_id: str
    mountpoint: Path
    handle: MountHandle
    refcount: int = 1

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

    def __init__(self, run_dir: str | Path, *, prefer_kernel: bool = False) -> None:
        self.run_dir = Path(run_dir)
        self.prefer_kernel = prefer_kernel
        self.mounts_dir = self.run_dir / "mounts"
        self.mounts_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, MountRecord] = {}

    def mount(self, manifest: ChildManifest) -> MountRecord:
        existing = self._records.get(manifest.id)
        if existing and existing.handle.mounted:
            existing.refcount += 1
            return existing
        if not manifest.pack_stack.lowers:
            raise ChildMountError(f"manifest {manifest.id} has no pack lowers")
        pack = manifest.pack_stack.lowers[-1]
        mountpoint = self.mounts_dir / _safe_name(manifest.id)
        handle = mount_ro(pack.path, mountpoint, prefer_kernel=self.prefer_kernel)
        record = MountRecord(manifest_id=manifest.id, mountpoint=mountpoint, handle=handle)
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

    def status(self, manifest: ChildManifest) -> dict[str, Any]:
        record = self._records.get(manifest.id)
        if record is None:
            return {"mounted": False, "refcount": 0, "mountpoint": ""}
        return record.to_dict()

    def stop_all(self) -> None:
        for manifest_id in list(self._records):
            record = self._records.pop(manifest_id)
            record.handle.unmount()
