"""Shared NFS overlay bookkeeping helpers.

Phase 03 keeps this deliberately filesystem-simple: upper directories live on
shared NFS, and this module tracks dirty size/count and rotates an active upper
into a sealed generation for commit. The actual union mount backend is a later
FUSE/runtime concern.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OverlayPaths:
    root: Path
    active_upper: Path
    sealed_dir: Path

    @classmethod
    def for_child(cls, overlays_root: str | Path, child_id: str) -> OverlayPaths:
        child_root = Path(overlays_root) / _safe_name(child_id)
        return cls(
            root=child_root,
            active_upper=child_root / "active",
            sealed_dir=child_root / "sealed",
        )


@dataclass(frozen=True)
class DirtyStats:
    dirty: bool
    file_count: int
    bytes: int


@dataclass(frozen=True)
class SealedOverlay:
    path: Path
    generation: int


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "child"


def ensure_active_upper(paths: OverlayPaths) -> Path:
    paths.active_upper.mkdir(parents=True, exist_ok=True)
    paths.sealed_dir.mkdir(parents=True, exist_ok=True)
    return paths.active_upper


def dirty_stats(path: str | Path) -> DirtyStats:
    upper = Path(path)
    if not upper.exists():
        return DirtyStats(dirty=False, file_count=0, bytes=0)
    count = 0
    total = 0
    for entry in upper.rglob("*"):
        if entry.is_file() or entry.is_symlink():
            count += 1
            try:
                total += entry.lstat().st_size
            except OSError:
                pass
    return DirtyStats(dirty=count > 0, file_count=count, bytes=total)


def seal_active_upper(paths: OverlayPaths, *, generation: int) -> SealedOverlay:
    ensure_active_upper(paths)
    paths.sealed_dir.mkdir(parents=True, exist_ok=True)
    sealed = paths.sealed_dir / f"g{generation:04d}-{int(time.time() * 1000)}"
    if paths.active_upper.exists():
        shutil.move(str(paths.active_upper), sealed)
    else:
        sealed.mkdir(parents=True, exist_ok=True)
    paths.active_upper.mkdir(parents=True, exist_ok=True)
    return SealedOverlay(path=sealed, generation=generation)


def cleanup_sealed(sealed: SealedOverlay) -> None:
    shutil.rmtree(sealed.path, ignore_errors=True)
