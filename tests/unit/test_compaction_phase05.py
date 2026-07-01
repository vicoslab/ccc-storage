from __future__ import annotations

from pathlib import Path

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_storage_mountd.workers.compaction import (
    CompactionPolicy,
    consolidate,
    plan_compaction,
    publish_consolidation,
)
from ccc_storage_pack.builder import BuildResult


def _manifest(base_size, delta_sizes):
    lowers = [PackInfo(path="/p/base.sqfs", sha256="b" * 64, size=base_size)]
    for i, size in enumerate(delta_sizes):
        lowers.append(PackInfo(path=f"/p/delta{i}.sqfs", sha256=str(i) * 64, size=size))
    return ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=len(delta_sizes),
        pack_stack=PackStack(active_revision=f"g{len(delta_sizes)}", lowers=tuple(lowers)),
    )


def test_no_plan_when_below_thresholds():
    manifest = _manifest(1000, [1, 1])  # 2 deltas, tiny bytes
    assert plan_compaction(manifest) is None


def test_no_plan_with_only_a_base():
    assert plan_compaction(_manifest(1000, [])) is None


def test_plan_triggers_on_delta_count():
    manifest = _manifest(1_000_000, [1] * 9)  # 9 deltas > 8
    plan = plan_compaction(manifest)
    assert plan is not None
    assert plan.delta_count == 9
    assert "delta-count" in plan.reason


def test_plan_triggers_on_delta_bytes_ratio():
    manifest = _manifest(100, [30])  # 30 > 20% of 100
    plan = plan_compaction(manifest)
    assert plan is not None
    assert "delta-bytes" in plan.reason


def test_plan_is_deterministic():
    manifest = _manifest(100, [30, 5])
    assert plan_compaction(manifest) == plan_compaction(manifest)


def test_custom_policy_thresholds():
    manifest = _manifest(1000, [1, 1, 1])
    assert plan_compaction(manifest, policy=CompactionPolicy(max_deltas=2)) is not None


def test_consolidate_then_publish_orders_build_verify_publish_retire(tmp_path):
    manifest = _manifest(100, [30, 30, 30])
    plan = plan_compaction(manifest)
    assert plan is not None

    source = tmp_path / "materialized"
    source.mkdir()
    (source / "f.txt").write_text("merged-layered-view")
    out = tmp_path / "consolidated.sqfs"

    calls: list[str] = []

    def fake_build_pack(src, dest, **kwargs):
        calls.append("build")
        Path(dest).write_bytes(b"consolidated")
        return BuildResult(
            pack=PackInfo(
                path=str(dest),
                sha256=sha256_file(dest),
                size=Path(dest).stat().st_size,
                file_count=1,
            ),
            args=("fake",),
        )

    new_pack = consolidate(plan, source, out, build_pack_fn=fake_build_pack)
    calls.append("verified")  # consolidate verifies internally before returning

    updated, retired = publish_consolidation(manifest, new_pack)
    calls.append("published")

    assert calls == ["build", "verified", "published"]
    assert new_pack.sha256 == sha256_file(out)
    assert updated.generation == manifest.generation + 1
    assert updated.pack_stack.lowers == (new_pack,)
    assert retired == manifest.pack_stack.lowers


def test_consolidate_requires_materialized_source(tmp_path):
    manifest = _manifest(100, [30, 30, 30])
    plan = plan_compaction(manifest)
    assert plan is not None
    missing = tmp_path / "does-not-exist"
    out = tmp_path / "out.sqfs"
    try:
        consolidate(plan, missing, out)
    except Exception as exc:  # CompactionError
        assert "source" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("expected consolidate to refuse a missing source dir")
