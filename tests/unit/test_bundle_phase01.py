from __future__ import annotations

import json
import tarfile

from ccc_storage_pack.bundle import BundleEntry, create_tar_bundle


def test_create_tar_bundle_contains_manifest_and_entries(tmp_path):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"pack")
    out = tmp_path / "bundle.tar"

    create_tar_bundle(out, [BundleEntry(str(pack), "packs/p.sqfs")], {"id": "dataset:foo"})

    with tarfile.open(out) as tar:
        names = set(tar.getnames())
        assert "manifest.json" in names
        assert "packs/p.sqfs" in names
        manifest = json.load(tar.extractfile("manifest.json"))  # type: ignore[arg-type]
    assert manifest["id"] == "dataset:foo"
