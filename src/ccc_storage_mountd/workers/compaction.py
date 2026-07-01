"""Delta-pack compaction planner + a safe build/publish skeleton (D-11).

Trigger (D-11): **>8 delta packs OR total delta bytes >20% of the base**. The
planner is a pure, deterministic function of the manifest's pack stack.

Honest scope note: actually *merging* base+deltas into one consolidated pack
requires a materialized layered view (a mounted union), which is a FUSE/runtime
concern out of reach in headless unit tests. :func:`consolidate` therefore
demands a caller-provided materialized source dir and reuses ``build_pack``;
:func:`publish_consolidation` performs the manifest swap. The ordering is
build → verify → publish → retire, matching the phase-03 RK-9 discipline, so a
crash before publish leaves the existing base+deltas intact.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_storage_pack.builder import BuildResult, build_pack
from ccc_storage_pack.reader import extract
from ccc_storage_pack.verify import verify_pack


class CompactionError(RuntimeError):
    """Raised when a compaction build cannot proceed safely."""


@dataclass(frozen=True)
class CompactionPolicy:
    max_deltas: int = 8
    delta_bytes_ratio: float = 0.20


@dataclass(frozen=True)
class CompactionPlan:
    child_id: str
    base: PackInfo
    deltas: tuple[PackInfo, ...]
    reason: str

    @property
    def delta_count(self) -> int:
        return len(self.deltas)


def plan_compaction(
    manifest: ChildManifest, *, policy: CompactionPolicy | None = None
) -> CompactionPlan | None:
    """Return a :class:`CompactionPlan` when the D-11 trigger fires, else ``None``."""
    policy = policy or CompactionPolicy()
    lowers = manifest.pack_stack.lowers
    if len(lowers) <= 1:
        return None
    base = lowers[0]
    deltas = lowers[1:]
    delta_bytes = sum(pack.size for pack in deltas)
    reasons: list[str] = []
    if len(deltas) > policy.max_deltas:
        reasons.append(f"delta-count>{policy.max_deltas}")
    if base.size > 0 and delta_bytes > base.size * policy.delta_bytes_ratio:
        reasons.append(f"delta-bytes>{int(policy.delta_bytes_ratio * 100)}%-base")
    if not reasons:
        return None
    return CompactionPlan(
        child_id=manifest.id,
        base=base,
        deltas=tuple(deltas),
        reason="+".join(reasons),
    )


def consolidate(
    plan: CompactionPlan,
    source_dir: str | Path,
    out_path: str | Path,
    *,
    build_pack_fn: Callable[..., BuildResult] = build_pack,
) -> PackInfo:
    """Build + verify one consolidated pack from a *materialized* layered tree.

    ``source_dir`` must already be the merged view of base+deltas (produced by a
    mounted union — not synthesized here). The new pack is verified against its
    freshly-computed metadata before it is returned; publishing is a separate
    step so a verify failure never mutates the manifest.
    """
    src = Path(source_dir)
    if not src.is_dir():
        raise CompactionError(f"materialized layered source dir required: {src}")
    result = build_pack_fn(src, out_path)
    verify_pack(out_path, result.pack)
    return result.pack


def publish_consolidation(
    manifest: ChildManifest,
    new_pack: PackInfo,
    *,
    new_generation: int | None = None,
) -> tuple[ChildManifest, tuple[PackInfo, ...]]:
    """Swap the pack stack to a single consolidated pack; return retired packs.

    The previous lowers are returned (not deleted) so the caller can hand them to
    the GC planner, which only retires packs that pass every safety predicate.
    """
    retired = manifest.pack_stack.lowers
    generation = manifest.generation + 1 if new_generation is None else new_generation
    updated = replace(
        manifest,
        generation=generation,
        pack_stack=PackStack(active_revision=f"g{generation}", lowers=(new_pack,)),
    )
    return updated, retired


def _selected_paths(selected: tuple[PackInfo, ...]) -> set[str]:
    return {pack.path for pack in selected}


def _generation_min(packs: tuple[PackInfo, ...]) -> int:
    values = [pack.generation_min for pack in packs if pack.generation_min]
    return min(values) if values else 0


def _generation_max(packs: tuple[PackInfo, ...]) -> int:
    values = [pack.generation_max for pack in packs if pack.generation_max]
    return max(values) if values else 0


def with_compaction_metadata(
    pack: PackInfo,
    *,
    selected: tuple[PackInfo, ...],
    target_level: int,
) -> PackInfo:
    """Return *pack* annotated as a compacted level pack."""
    return replace(
        pack,
        level=target_level,
        generation_min=_generation_min(selected),
        generation_max=_generation_max(selected),
        kind="compact",
    )


def publish_partial_compaction(
    manifest: ChildManifest,
    *,
    selected: tuple[PackInfo, ...],
    new_pack: PackInfo,
    target_level: int,
) -> tuple[ChildManifest, tuple[PackInfo, ...]]:
    """Replace only *selected* packs with one compacted pack.

    The pack stack is stored oldest→newest. Partial compaction candidates are a
    contiguous newest run; this function validates that shape and leaves every
    older/lower pack untouched.
    """
    selected = tuple(selected)
    if not selected:
        raise CompactionError("partial compaction requires at least one selected pack")
    lowers = manifest.pack_stack.lowers
    paths = _selected_paths(selected)
    selected_indexes = [idx for idx, pack in enumerate(lowers) if pack.path in paths]
    if len(selected_indexes) != len(selected):
        raise CompactionError("selected packs are not all present in the manifest")
    if selected_indexes != list(range(selected_indexes[0], selected_indexes[-1] + 1)):
        raise CompactionError("selected packs must be contiguous in the manifest stack")

    first = selected_indexes[0]
    last = selected_indexes[-1]
    retired = lowers[first : last + 1]
    annotated = with_compaction_metadata(
        new_pack,
        selected=retired,
        target_level=target_level,
    )
    updated_lowers = (*lowers[:first], annotated, *lowers[last + 1 :])
    updated = replace(
        manifest,
        pack_stack=PackStack(
            active_revision=manifest.pack_stack.active_revision,
            lowers=updated_lowers,
        ),
    )
    return updated, retired


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_entry(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dst.symlink_to(src.readlink())
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
    elif src.is_file():
        shutil.copy2(src, dst)


def _merge_extracted_layer(layer: Path, materialized: Path) -> None:
    """Overlay one extracted pack layer onto *materialized*.

    ``unsquashfs`` does not understand fuse-overlayfs ``.wh.*`` files, so a
    naive extract-old-then-extract-new merge can leave both ``victim`` and
    ``.wh.victim`` in the compacted pack. Apply whiteouts while still preserving
    the whiteout files as durable tombstones for untouched lower levels.
    """
    # Opaque directory markers hide every older entry in the same directory. The
    # layer's own files are copied in the second pass below.
    for opq in sorted(layer.rglob(".wh..wh..opq")):
        rel_dir = opq.parent.relative_to(layer)
        dst_dir = materialized / rel_dir
        if dst_dir.exists():
            for child in list(dst_dir.iterdir()):
                _remove_path(child)
        dst_dir.mkdir(parents=True, exist_ok=True)

    for entry in sorted(layer.rglob("*"), key=lambda item: (len(item.parts), item.as_posix())):
        rel = entry.relative_to(layer)
        dst = materialized / rel
        if entry.is_dir():
            if dst.exists() and not dst.is_dir():
                _remove_path(dst)
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if entry.name.startswith(".wh.") and entry.name != ".wh..wh..opq":
            target_name = entry.name[len(".wh.") :]
            _remove_path(dst.parent / target_name)
            _copy_entry(entry, dst)
            continue
        _copy_entry(entry, dst)


def build_partial_compaction(
    selected: tuple[PackInfo, ...],
    out_path: str | Path,
    *,
    target_level: int,
    extract_fn: Callable[..., None] = extract,
    build_pack_fn: Callable[..., BuildResult] = build_pack,
) -> PackInfo:
    """Extract selected packs old→new and build one target-level pack.

    This helper intentionally avoids live FUSE. It uses ``unsquashfs`` via
    :func:`ccc_storage_pack.reader.extract` by default, which is slower but
    works in background maintenance and in integration tests.
    """
    selected = tuple(selected)
    if not selected:
        raise CompactionError("partial compaction requires selected packs")
    with tempfile.TemporaryDirectory(prefix="ccc-partial-compact-") as tmp:
        tmp_path = Path(tmp)
        materialized = tmp_path / "view"
        materialized.mkdir(parents=True, exist_ok=True)
        for idx, pack in enumerate(selected):
            layer = tmp_path / f"layer-{idx:04d}"
            layer.mkdir(parents=True, exist_ok=True)
            extract_fn(pack.path, layer)
            _merge_extracted_layer(layer, materialized)
        result = build_pack_fn(materialized, out_path)
    verify_pack(out_path, result.pack)
    return with_compaction_metadata(
        result.pack,
        selected=selected,
        target_level=target_level,
    )
