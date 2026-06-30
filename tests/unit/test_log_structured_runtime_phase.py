from __future__ import annotations

import os
import shutil

import pytest

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_mountd.workers.compaction import build_partial_compaction
from ccc_layered_mountd.workers.levels import LevelPolicy, parse_levels, plan_level_compaction
from ccc_layered_pack.builder import build_pack
from ccc_layered_pack.reader import extract


def _require_squashfs_tools():
    missing = [name for name in ("mksquashfs", "unsquashfs") if not shutil.which(name)]
    if missing:
        pytest.skip(f"SquashFS tools missing: {', '.join(missing)}")


def test_mb_scale_log_structured_partial_compaction_with_real_squashfs(tmp_path):
    _require_squashfs_tools()
    policy = LevelPolicy(levels=parse_levels("0:8M,1:4M,2:1M,3:256K,4:64K"))

    base_src = tmp_path / "base-src"
    l3_src = tmp_path / "l3-src"
    l4_src = tmp_path / "l4-src"
    new_src = tmp_path / "new-src"
    for path in (base_src, l3_src, l4_src, new_src):
        path.mkdir()
    (base_src / "stable.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    (l3_src / "chunk-l3.bin").write_bytes(os.urandom(150 * 1024))
    (l3_src / "deleted-from-base").write_text("older selected copy")
    (l4_src / ".wh.deleted-from-base").write_text("")
    (l4_src / "chunk-l4.bin").write_bytes(os.urandom(48 * 1024))
    (new_src / "chunk-new.bin").write_bytes(os.urandom(48 * 1024))

    base = build_pack(base_src, tmp_path / "base.sqfs").pack
    l3 = build_pack(l3_src, tmp_path / "l3.sqfs").pack
    l4 = build_pack(l4_src, tmp_path / "l4.sqfs").pack
    new = build_pack(new_src, tmp_path / "new.sqfs").pack
    base = PackInfo(
        **(base.to_dict() | {"level": 0, "generation_min": 1, "generation_max": 1})
    )
    l3 = PackInfo(
        **(
            l3.to_dict()
            | {"level": 3, "generation_min": 2, "generation_max": 2, "kind": "delta"}
        )
    )
    l4 = PackInfo(
        **(
            l4.to_dict()
            | {"level": 4, "generation_min": 3, "generation_max": 3, "kind": "delta"}
        )
    )
    new = PackInfo(
        **(
            new.to_dict()
            | {"level": 4, "generation_min": 4, "generation_max": 4, "kind": "delta"}
        )
    )
    manifest = ChildManifest(
        id="dataset:runtime",
        name="runtime",
        type="dataset",
        generation=4,
        pack_stack=PackStack(active_revision="g4", lowers=(base, l3, l4, new)),
    )

    candidate = plan_level_compaction(manifest.pack_stack.lowers, policy)
    assert candidate is not None
    assert base not in candidate.packs

    compact = build_partial_compaction(
        tuple(candidate.packs),
        tmp_path / "compact.sqfs",
        target_level=candidate.target_level,
    )
    extracted = tmp_path / "extracted"
    extract(compact.path, extracted)

    assert (extracted / ".wh.deleted-from-base").exists()
    assert not (extracted / "deleted-from-base").exists()
    assert (extracted / "chunk-l3.bin").exists()
    assert compact.generation_min == 2
    assert compact.generation_max == 4


@pytest.mark.slow
def test_large_log_structured_compaction_does_not_rewrite_base(tmp_path):
    _require_squashfs_tools()
    if os.environ.get("CCC_RUN_LARGE_LOG_COMPACTION_TEST") != "1":
        pytest.skip("set CCC_RUN_LARGE_LOG_COMPACTION_TEST=1 to run the ~1GiB validation")

    base = PackInfo(
        path=str(tmp_path / "base.sqfs"),
        sha256="b" * 64,
        size=900 * 1024**2,
        level=0,
        generation_min=1,
        generation_max=1,
        kind="base",
    )
    l2 = PackInfo(
        path=str(tmp_path / "l2.sqfs"),
        sha256="2" * 64,
        size=96 * 1024**2,
        level=2,
        generation_min=2,
        generation_max=2,
        kind="delta",
    )
    l3 = PackInfo(
        path=str(tmp_path / "l3.sqfs"),
        sha256="3" * 64,
        size=24 * 1024**2,
        level=3,
        generation_min=3,
        generation_max=3,
        kind="delta",
    )
    new = PackInfo(
        path=str(tmp_path / "new.sqfs"),
        sha256="4" * 64,
        size=24 * 1024**2,
        level=3,
        generation_min=4,
        generation_max=4,
        kind="delta",
    )
    policy = LevelPolicy(levels=parse_levels("0:2G,1:512M,2:128M,3:32M,4:8M"))

    candidate = plan_level_compaction((base, l2, l3, new), policy)

    assert candidate is not None
    assert base not in candidate.packs
    assert set(candidate.packs) == {l2, l3, new}
    assert candidate.target_level == 1
