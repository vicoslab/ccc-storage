"""Shared NFS overlay and local-async dirty-state helpers.

Phase 03 kept this deliberately filesystem-simple: shared-NFS uppers live on
NFS.  Per-child write policies now also need node-local SSD uppers plus an NFS
logical dirty mirror for async visibility.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccc_storage_core.manifest import ChildManifest
from ccc_storage_core.names import safe_namespace_name
from ccc_storage_core.resolve import resolve_owner_path


@dataclass(frozen=True)
class OverlayPaths:
    root: Path
    active_upper: Path
    sealed_dir: Path

    @property
    def work(self) -> Path:
        return self.root / "work"

    @classmethod
    def for_child(cls, overlays_root: str | Path, child_id: str) -> OverlayPaths:
        child_root = Path(overlays_root) / _safe_name(child_id)
        return cls(
            root=child_root,
            active_upper=child_root / "active",
            sealed_dir=child_root / "sealed",
        )


@dataclass(frozen=True)
class LocalOverlayPaths:
    root: Path
    active_upper: Path
    work: Path
    meta: Path

    @classmethod
    def for_child(cls, local_root: str | Path, child_id: str) -> LocalOverlayPaths:
        child_root = Path(local_root) / _safe_name(child_id)
        return cls(
            root=child_root,
            active_upper=child_root / "active",
            work=child_root / "work",
            meta=child_root / "meta.json",
        )


@dataclass(frozen=True)
class DirtyMirrorPaths:
    root: Path
    epochs_dir: Path
    current: Path
    publish_json: Path

    @classmethod
    def for_child(cls, nfs_root: str | Path, child_id: str) -> DirtyMirrorPaths:
        root = Path(nfs_root) / "async" / _safe_name(child_id)
        return cls(
            root=root,
            epochs_dir=root / "epochs",
            current=root / "current",
            publish_json=root / "publish.json",
        )


@dataclass(frozen=True)
class DirtyMirror:
    child_id: str
    node_id: str
    epoch: int
    base_generation: int
    path: Path
    file_count: int
    bytes: int
    published_ts: float


@dataclass(frozen=True)
class DirtyStats:
    dirty: bool
    file_count: int
    bytes: int


@dataclass(frozen=True)
class SealedOverlay:
    path: Path
    generation: int


@dataclass(frozen=True)
class OverlayRoute:
    """The overlay a write should land in, per the nearest owning boundary."""

    owner_id: str
    is_parent: bool
    overlay: OverlayPaths
    inner_path: str


def route_path(
    parent_manifest: ChildManifest,
    rel_path: str,
    overlays_root: str | Path,
) -> OverlayRoute:
    """Route a write at *rel_path* to the nearest owning boundary's overlay.

    A write under a child boundary lands in that child's overlay; a write
    outside every boundary lands in the parent overlay (design routing rules).
    """
    owner = resolve_owner_path(
        parent_manifest.child_boundaries, rel_path, parent_id=parent_manifest.id
    )
    return OverlayRoute(
        owner_id=owner.owner_id,
        is_parent=owner.is_parent,
        overlay=OverlayPaths.for_child(overlays_root, owner.owner_id),
        inner_path=owner.inner_path,
    )


def _safe_name(value: str) -> str:
    return safe_namespace_name(value)


def _is_overlayfs_artifact(path: Path) -> bool:
    return path.name.startswith(".wh.")


def ensure_active_upper(paths: OverlayPaths) -> Path:
    paths.active_upper.mkdir(parents=True, exist_ok=True)
    paths.sealed_dir.mkdir(parents=True, exist_ok=True)
    return paths.active_upper


def ensure_local_upper(paths: LocalOverlayPaths) -> Path:
    paths.active_upper.mkdir(parents=True, exist_ok=True)
    paths.work.mkdir(parents=True, exist_ok=True)
    return paths.active_upper


def dirty_stats(path: str | Path) -> DirtyStats:
    upper = Path(path)
    if not upper.exists():
        return DirtyStats(dirty=False, file_count=0, bytes=0)
    count = 0
    total = 0
    for entry in upper.rglob("*"):
        if _is_overlayfs_artifact(entry):
            continue
        if entry.is_file() or entry.is_symlink():
            count += 1
            try:
                total += entry.lstat().st_size
            except OSError:
                pass
    return DirtyStats(dirty=count > 0, file_count=count, bytes=total)


def local_overlay_paths(local_root: str | Path, child_id: str) -> LocalOverlayPaths:
    return LocalOverlayPaths.for_child(local_root, child_id)


def dirty_mirror_paths(nfs_root: str | Path, child_id: str) -> DirtyMirrorPaths:
    return DirtyMirrorPaths.for_child(nfs_root, child_id)


def _read_publish_json(paths: DirtyMirrorPaths) -> dict[str, Any] | None:
    try:
        return json.loads(paths.publish_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _next_epoch(paths: DirtyMirrorPaths) -> int:
    latest = _read_publish_json(paths)
    return int(latest.get("epoch", 0)) + 1 if latest else 1


def _copy_logical_tree(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = dest / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        elif entry.is_symlink():
            target.symlink_to(os.readlink(entry))
        elif entry.is_file():
            shutil.copy2(entry, target)


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def publish_logical_mirror(
    source: str | Path,
    paths: DirtyMirrorPaths,
    *,
    child_id: str,
    node_id: str,
    base_generation: int,
) -> DirtyMirror:
    """Publish a complete logical dirty mirror epoch to shared NFS state."""

    src = Path(source)
    if not src.is_dir():
        raise FileNotFoundError(f"logical mirror source is not a directory: {src}")
    paths.epochs_dir.mkdir(parents=True, exist_ok=True)
    epoch = _next_epoch(paths)
    epoch_name = f"e{epoch:06d}"
    staging = paths.epochs_dir / f".{epoch_name}.tmp"
    final = paths.epochs_dir / epoch_name
    if staging.exists():
        shutil.rmtree(staging)
    if final.exists():
        shutil.rmtree(final)
    tree = staging / "tree"
    _copy_logical_tree(src, tree)
    stats = dirty_stats(tree)
    published_ts = time.time()
    metadata: dict[str, object] = {
        "child_id": child_id,
        "node_id": node_id,
        "epoch": epoch,
        "base_generation": base_generation,
        "file_count": stats.file_count,
        "bytes": stats.bytes,
        "published_ts": published_ts,
    }
    _atomic_write_json(staging / "epoch.json", metadata)
    os.replace(staging, final)
    latest_tmp = paths.current.with_name(f".{paths.current.name}.tmp")
    if latest_tmp.exists() or latest_tmp.is_symlink():
        latest_tmp.unlink()
    latest_tmp.symlink_to(final / "tree")
    os.replace(latest_tmp, paths.current)
    _atomic_write_json(paths.publish_json, {**metadata, "path": str(final / "tree")})
    return DirtyMirror(
        child_id=child_id,
        node_id=node_id,
        epoch=epoch,
        base_generation=base_generation,
        path=final / "tree",
        file_count=stats.file_count,
        bytes=stats.bytes,
        published_ts=published_ts,
    )


def latest_dirty_mirror(nfs_root: str | Path, child_id: str) -> DirtyMirror | None:
    paths = dirty_mirror_paths(nfs_root, child_id)
    data = _read_publish_json(paths)
    if not data:
        return None
    mirror_path = Path(str(data.get("path") or paths.current))
    if not mirror_path.exists() and paths.current.exists():
        mirror_path = paths.current
    if not mirror_path.exists():
        return None
    return DirtyMirror(
        child_id=str(data.get("child_id", child_id)),
        node_id=str(data.get("node_id", "")),
        epoch=int(data.get("epoch", 0)),
        base_generation=int(data.get("base_generation", 0)),
        path=mirror_path,
        file_count=int(data.get("file_count", 0)),
        bytes=int(data.get("bytes", 0)),
        published_ts=float(data.get("published_ts", 0.0)),
    )


def cleanup_dirty_mirror(nfs_root: str | Path, child_id: str) -> None:
    shutil.rmtree(dirty_mirror_paths(nfs_root, child_id).root, ignore_errors=True)


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
