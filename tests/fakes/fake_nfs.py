"""Fake-NFS — a workspace dir laid out as ``/storage/.ccc-storage``.

Because the workspace is itself NFS-backed, this gives realistic close-to-open
semantics, real ``O_EXCL`` atomicity, and real rename atomicity for free,
without touching any real dataset. The directory lives under ``$CCC_TEST_ROOT``
(asserted inside the workspace by the isolation guard).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The five authoritative shared-state subdirectories (planning README §1, §5).
SUBDIRS = ("registry", "packs", "overlays", "locks", "events")


@dataclass
class FakeNfs:
    """A single fake shared-NFS tree."""

    root: Path
    ccc_storage: Path

    def subdir(self, name: str) -> Path:
        if name not in SUBDIRS:
            raise KeyError(f"unknown .ccc-storage subdir: {name!r}")
        return self.ccc_storage / name

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def create_fake_nfs(test_root: str | Path) -> FakeNfs:
    """Create a fresh, uniquely-named fake-NFS tree under ``<test_root>/fake-nfs``."""
    base = Path(test_root) / "fake-nfs"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=base, prefix="nfs-"))
    ccc_storage = root / ".ccc-storage"
    for name in SUBDIRS:
        (ccc_storage / name).mkdir(parents=True, exist_ok=True)
    return FakeNfs(root=root, ccc_storage=ccc_storage)
