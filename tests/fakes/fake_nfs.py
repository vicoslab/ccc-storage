"""Fake-NFS — a workspace dir laid out as ``/storage/.ccc-layered``.

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
    ccc_layered: Path

    def subdir(self, name: str) -> Path:
        if name not in SUBDIRS:
            raise KeyError(f"unknown .ccc-layered subdir: {name!r}")
        return self.ccc_layered / name

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def create_fake_nfs(test_root: str | Path) -> FakeNfs:
    """Create a fresh, uniquely-named fake-NFS tree under ``<test_root>/fake-nfs``."""
    base = Path(test_root) / "fake-nfs"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=base, prefix="nfs-"))
    ccc_layered = root / ".ccc-layered"
    for name in SUBDIRS:
        (ccc_layered / name).mkdir(parents=True, exist_ok=True)
    return FakeNfs(root=root, ccc_layered=ccc_layered)
