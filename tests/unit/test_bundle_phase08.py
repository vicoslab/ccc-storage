from __future__ import annotations

import pytest

from ccc_layered_pack.bundle import (
    BundleEntry,
    MountGraph,
    MountGraphNode,
    PacksetVerificationError,
    build_packset_bundle,
    unpack_packset_bundle,
    verify_packset_dir,
)


def test_packset_bundle_roundtrip_contains_mount_graph_and_checksums(tmp_path):
    pack = tmp_path / "env-a.sqfs"
    pack.write_bytes(b"pack-bytes")
    graph = MountGraph(
        root="home",
        included=(
            MountGraphNode(child_id="home", path="."),
            MountGraphNode(child_id="env:a", path="conda/envs/a"),
        ),
        excluded=(MountGraphNode(child_id="env:b", path="conda/envs/b", reason="not selected"),),
    )
    bundle = build_packset_bundle(
        tmp_path / "bundle.tar", [BundleEntry(str(pack), "packs/env-a.sqfs")], graph
    )

    unpacked = unpack_packset_bundle(bundle, tmp_path / "unpacked")

    assert unpacked.graph.root == "home"
    assert unpacked.graph.excluded[0].child_id == "env:b"
    assert (tmp_path / "unpacked" / "packs" / "env-a.sqfs").read_bytes() == b"pack-bytes"


def test_packset_verify_detects_tampered_payload(tmp_path):
    pack = tmp_path / "env-a.sqfs"
    pack.write_bytes(b"pack-bytes")
    graph = MountGraph(
        root="home", included=(MountGraphNode(child_id="env:a", path="conda/envs/a"),)
    )
    bundle = build_packset_bundle(
        tmp_path / "bundle.tar", [BundleEntry(str(pack), "packs/env-a.sqfs")], graph
    )
    unpack_packset_bundle(bundle, tmp_path / "unpacked")
    (tmp_path / "unpacked" / "packs" / "env-a.sqfs").write_bytes(b"tampered")

    with pytest.raises(PacksetVerificationError):
        verify_packset_dir(tmp_path / "unpacked")
