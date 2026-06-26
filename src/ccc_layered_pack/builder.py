"""SquashFS pack builder wrapper."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.manifest import PackInfo
from ccc_layered_pack.verify import inspect_pack


class PackBuildError(RuntimeError):
    """Raised when pack construction fails."""


@dataclass(frozen=True)
class BuildResult:
    pack: PackInfo
    args: tuple[str, ...]


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
    records only added/modified files by packing the sealed upper as-is.
    """
    _ = base_manifest, tombstones
    return build_pack(src, out, comp=comp, block=block)


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
    args = [
        exe,
        str(src_path),
        str(out_path),
        "-noappend",
        "-no-progress",
        "-comp",
        comp,
        "-b",
        block,
    ]
    for boundary in exclude_boundaries or []:
        args.extend(["-e", boundary.strip("/")])

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
