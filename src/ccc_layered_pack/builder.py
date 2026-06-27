"""SquashFS pack builder wrapper."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.manifest import PackInfo
from ccc_layered_pack.verify import inspect_pack


class PackBuildError(RuntimeError):
    """Raised when pack construction fails."""


# Internal marker dropped at an excluded child-boundary path so the parent tree
# stays navigable before the child is mounted. Hidden from user-facing listings
# (it starts with a dot, so ``managed_parent.is_internal_name`` filters it).
BOUNDARY_MARKER_NAME = ".ccc-boundary"
_OVERLAY_WHITEOUT_RE = re.compile(r"^\.wh\..+")


def safe_pack_name(child_id: str) -> str:
    """Filesystem-safe namespace for one managed child/root pack object set."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", child_id).strip("_") or "child"


def pack_object_dir(packs_root: str | Path, child_id: str) -> Path:
    """Directory that stores SquashFS objects for exactly one manifest id.

    Nested child packs live beside the parent pack namespace under the shared
    packs root; they are not stored inside the parent's SquashFS payload.
    """
    return Path(packs_root) / safe_pack_name(child_id)


def is_overlayfs_artifact(path: str | Path) -> bool:
    """True for overlay/fuse-overlayfs whiteout metadata, not user files.

    Current delta packs record added/modified regular content. Deletion
    tombstones require an explicit whiteout-aware format and must not leak the
    implementation files (``.wh.*``) into user-visible SquashFS layers.
    """
    return bool(_OVERLAY_WHITEOUT_RE.match(Path(path).name))


def prepare_delta_source(src: str | Path, dst: str | Path) -> int:
    """Copy a sealed overlay upper into *dst*, filtering overlay metadata.

    Returns the number of copied regular files/symlinks. Directories are
    recreated, ``.wh.*`` whiteout artifacts are skipped, and unsupported special
    files are ignored so they cannot leak into immutable user-visible packs.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.is_dir():
        raise PackBuildError(f"source directory does not exist: {src_path}")
    copied = 0
    dst_path.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src_path.rglob("*")):
        rel = entry.relative_to(src_path)
        if any(is_overlayfs_artifact(part) for part in rel.parts):
            continue
        target = dst_path / rel
        if entry.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(entry), target)
            copied += 1
        elif entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            copied += 1
        # FIFOs/devices/sockets are overlay implementation details for now.
    return copied


@dataclass(frozen=True)
class BuildResult:
    pack: PackInfo
    args: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryMarkerPlan:
    boundary_paths: tuple[str, ...]
    marker_files: tuple[str, ...]


def plan_boundary_markers(boundaries: list[str] | None) -> BoundaryMarkerPlan:
    """Plan the boundary dirs + marker files for the excluded child subtrees."""
    paths = tuple(item.strip("/") for item in (boundaries or []) if item.strip("/"))
    markers = tuple(f"{path}/{BOUNDARY_MARKER_NAME}" for path in paths)
    return BoundaryMarkerPlan(boundary_paths=paths, marker_files=markers)


def create_boundary_markers(src: str | Path, boundaries: list[str] | None) -> list[Path]:
    """Create empty boundary dirs + marker files under *src*.

    This emits only navigation stubs (an empty dir plus an internal marker file)
    at each child-boundary path; it never copies child payload, so the parent
    pack stays free of duplicated child contents (D-13).
    """
    root = Path(src)
    created: list[Path] = []
    for boundary_path in plan_boundary_markers(boundaries).boundary_paths:
        boundary_dir = root / boundary_path
        boundary_dir.mkdir(parents=True, exist_ok=True)
        marker = boundary_dir / BOUNDARY_MARKER_NAME
        marker.write_text("")
        created.append(marker)
    return created


def prepare_parent_source(
    src: str | Path,
    dst: str | Path,
    *,
    exclude_boundaries: list[str] | None = None,
) -> None:
    """Copy parent-owned files to *dst* and emit child-boundary stubs.

    Child payload under every boundary is deliberately omitted. The boundary
    directory itself is kept with an internal marker so the mounted parent pack
    has a real mountpoint where the child SquashFS can be mounted later.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    excludes = tuple(
        item.strip("/") for item in (exclude_boundaries or []) if item.strip("/")
    )
    dst_path.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src_path.rglob("*")):
        rel = entry.relative_to(src_path).as_posix()
        if any(rel == excluded or rel.startswith(excluded + "/") for excluded in excludes):
            continue
        target = dst_path / rel
        if entry.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
    create_boundary_markers(dst_path, list(excludes))


def count_files(src: str | Path, *, exclude_boundaries: list[str] | None = None) -> int:
    """Count regular files below *src*, excluding nested child-pack boundaries."""
    root = Path(src)
    excludes = tuple(item.strip("/") for item in (exclude_boundaries or []))
    count = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if any(rel == excluded or rel.startswith(excluded + "/") for excluded in excludes):
            continue
        if path.is_file():
            count += 1
    return count


def build_delta(
    src: str | Path,
    base_manifest: object,
    out: str | Path,
    tombstones: list[str] | None = None,
    *,
    comp: str = "zstd",
    block: str = "1M",
) -> BuildResult:
    """Build a delta pack from a sealed overlay upper.

    Tombstones are reserved for the later whiteout-aware implementation. Phase 03
    records added/modified user files while filtering fuse-overlayfs ``.wh.*``
    implementation artifacts so they do not leak into immutable lower packs.
    """
    _ = base_manifest, tombstones
    with tempfile.TemporaryDirectory(prefix="ccc-delta-src-") as tmp:
        prepared = Path(tmp) / "upper"
        prepare_delta_source(src, prepared)
        return build_pack(prepared, out, comp=comp, block=block)


def build_pack(
    src: str | Path,
    out: str | Path,
    *,
    comp: str = "zstd",
    block: str = "1M",
    exclude_boundaries: list[str] | None = None,
) -> BuildResult:
    """Build a SquashFS pack from *src* into *out* using `mksquashfs`."""
    src_path = Path(src)
    out_path = Path(out)
    if not src_path.is_dir():
        raise PackBuildError(f"source directory does not exist: {src_path}")
    exe = shutil.which("mksquashfs")
    if not exe:
        raise PackBuildError("mksquashfs not found; install squashfs-tools in the ccc-dev env")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ccc-pack-src-") as tmp:
        pack_src = src_path
        if exclude_boundaries:
            pack_src = Path(tmp) / "parent"
            prepare_parent_source(
                src_path,
                pack_src,
                exclude_boundaries=exclude_boundaries,
            )
        args = [
            exe,
            str(pack_src),
            str(out_path),
            "-noappend",
            "-no-progress",
            "-comp",
            comp,
            "-b",
            block,
        ]

        cp = subprocess.run(args, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        msg = cp.stderr.strip() or cp.stdout.strip()
        raise PackBuildError(f"mksquashfs failed ({cp.returncode}): {msg}")

    inspected = inspect_pack(
        out_path,
        file_count=count_files(src_path, exclude_boundaries=exclude_boundaries),
    )
    info = PackInfo(
        path=str(out_path),
        sha256=inspected.sha256,
        size=inspected.size,
        file_count=inspected.file_count,
        block=block,
        comp=comp,
    )
    return BuildResult(pack=info, args=tuple(args))
