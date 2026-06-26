from __future__ import annotations

import pytest

from ccc_layered_core.manifest import (
    ChildBoundary,
    ChildManifest,
    OverlayInfo,
    PackInfo,
    PackStack,
    S3Info,
    UnsupportedSchemaVersion,
    dump_atomic,
    load_manifest,
)


def _sample_manifest() -> ChildManifest:
    return ChildManifest(
        id="user-root:alice@example.org",
        name="alice@example.org",
        type="user-root",
        generation=12,
        state="dirty",
        pack_stack=PackStack(
            active_revision="g12",
            lowers=(
                PackInfo(
                    path="/packs/users/alice/root-g12.sqfs",
                    sha256="a" * 64,
                    size=123456,
                    file_count=42,
                    block="1M",
                    comp="zstd",
                ),
            ),
        ),
        overlay=OverlayInfo(
            mode="shared-overlay",
            active_upper="/overlays/users/alice/active",
            overlay_generation=3,
        ),
        s3=S3Info(
            pack_state="available",
            snapshot_state="stale",
            pack_generation=12,
            overlay_generation=3,
            uri="s3://bucket/users/alice/root-g12.sqfs",
        ),
        child_boundaries=(
            ChildBoundary("conda/envs/env-a", "conda-env:alice:env-a"),
            ChildBoundary("conda/envs/env-b", "conda-env:alice:env-b", export_policy="recursive"),
        ),
    )


def test_manifest_roundtrip_preserves_pack_stack_overlay_s3_and_child_boundaries(tmp_path):
    path = tmp_path / "child.toml"
    original = _sample_manifest()

    dump_atomic(path, original)
    loaded = load_manifest(path)

    assert loaded == original
    assert [b.path for b in loaded.child_boundaries] == ["conda/envs/env-a", "conda/envs/env-b"]
    assert loaded.pack_stack.lowers[0].sha256 == "a" * 64


def test_dump_atomic_replaces_manifest_without_leaving_temp_files(tmp_path):
    path = tmp_path / "manifest.toml"
    dump_atomic(path, _sample_manifest())
    first = path.read_text()

    updated = ChildManifest(id="dataset:foo", name="foo", type="dataset", generation=2)
    dump_atomic(path, updated)

    assert path.read_text() != first
    assert load_manifest(path).generation == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_manifest_rejects_unsupported_newer_schema(tmp_path):
    path = tmp_path / "future.toml"
    path.write_text(
        'schema_version = 999\nid = "x"\nname = "x"\ntype = "dataset"\ngeneration = 1\n'
    )

    with pytest.raises(UnsupportedSchemaVersion):
        load_manifest(path)
