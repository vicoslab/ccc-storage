from __future__ import annotations

from ccc_storage_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)


def test_pack_info_defaults_for_old_manifest():
    # Old manifests have no level metadata: default to base/level 0, gen 0.
    pack = PackInfo.from_dict(
        {"path": "/p/base.sqfs", "sha256": "a" * 64, "size": 1024}
    )
    assert pack.level == 0
    assert pack.generation_min == 0
    assert pack.generation_max == 0
    assert pack.kind == "base"


def test_pack_info_to_dict_omits_default_level_metadata():
    pack = PackInfo(path="/p/base.sqfs", sha256="a" * 64, size=1024)
    data = pack.to_dict()
    # Backwards compatible: defaults are not emitted, so old readers are happy.
    assert "level" not in data
    assert "generation_min" not in data
    assert "generation_max" not in data
    assert "kind" not in data


def test_pack_info_to_dict_includes_non_default_level_metadata():
    pack = PackInfo(
        path="/p/delta-g0021.sqfs",
        sha256="b" * 64,
        size=6 * 1024 * 1024,
        level=4,
        generation_min=21,
        generation_max=21,
        kind="delta",
    )
    data = pack.to_dict()
    assert data["level"] == 4
    assert data["generation_min"] == 21
    assert data["generation_max"] == 21
    assert data["kind"] == "delta"


def test_pack_info_from_dict_reads_level_metadata():
    pack = PackInfo.from_dict(
        {
            "path": "/p/level2.sqfs",
            "sha256": "c" * 64,
            "size": 734003200,
            "level": 2,
            "generation_min": 12,
            "generation_max": 20,
            "kind": "compacted",
        }
    )
    assert pack.level == 2
    assert pack.generation_min == 12
    assert pack.generation_max == 20
    assert pack.kind == "compacted"


def test_manifest_roundtrips_level_metadata(tmp_path):
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=21,
        pack_stack=PackStack(
            active_revision="g21",
            lowers=(
                PackInfo(path="/p/base.sqfs", sha256="a" * 64, size=1 << 30, level=0),
                PackInfo(
                    path="/p/level2.sqfs",
                    sha256="c" * 64,
                    size=734003200,
                    level=2,
                    generation_min=12,
                    generation_max=20,
                    kind="compacted",
                ),
                PackInfo(
                    path="/p/delta-g0021.sqfs",
                    sha256="b" * 64,
                    size=6 * 1024 * 1024,
                    level=4,
                    generation_min=21,
                    generation_max=21,
                    kind="delta",
                ),
            ),
        ),
    )
    path = tmp_path / "child.toml"
    dump_atomic(path, manifest)
    loaded = load_manifest(path)
    assert loaded == manifest
    assert loaded.pack_stack.lowers[1].level == 2
    assert loaded.pack_stack.lowers[2].kind == "delta"
    assert loaded.pack_stack.lowers[2].generation_max == 21
