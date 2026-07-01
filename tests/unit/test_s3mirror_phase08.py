from __future__ import annotations

from dataclasses import replace

import pytest

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, S3Info, dump_atomic
from ccc_storage_hpc.object_store import LocalObjectStore, ObjectStoreError
from ccc_storage_hpc.s3mirror import RecallError, mirror_committed_packs, recall_cold_pack


def _manifest_with_pack(tmp_path):
    pack = tmp_path / "packs" / "env-a.sqfs"
    pack.parent.mkdir()
    pack.write_bytes(b"env-a-pack")
    manifest = ChildManifest(
        id="env:a",
        name="env-a",
        type="conda-env",
        generation=3,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),)
        ),
    )
    manifest_path = tmp_path / "registry" / "env-a.toml"
    dump_atomic(manifest_path, manifest)
    return manifest, manifest_path, pack


def test_mirror_committed_packs_uploads_pack_and_manifest_idempotently(tmp_path):
    manifest, manifest_path, pack = _manifest_with_pack(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")

    first = mirror_committed_packs(manifest, manifest_path, store, prefix="ccc/env-a")
    second = mirror_committed_packs(manifest, manifest_path, store, prefix="ccc/env-a")

    assert first.uploaded_keys == second.uploaded_keys
    assert store.read_bytes("ccc/env-a/packs/env-a.sqfs") == pack.read_bytes()
    assert b"env:a" in store.read_bytes("ccc/env-a/manifest.toml")
    assert first.manifest.s3.pack_state == "hot"
    assert first.manifest.s3.uri == "ccc/env-a"


def test_mirror_failure_does_not_change_authoritative_manifest(tmp_path):
    manifest, manifest_path, _pack = _manifest_with_pack(tmp_path)

    class FailingStore(LocalObjectStore):
        def put_file(self, key, source):  # type: ignore[no-untyped-def]
            raise ObjectStoreError("upload failed")

    with pytest.raises(ObjectStoreError):
        mirror_committed_packs(
            manifest, manifest_path, FailingStore(tmp_path / "objects"), prefix="bad"
        )

    assert manifest.pack_stack.lowers[0].path.endswith("env-a.sqfs")
    assert 'pack_state = "missing"' in manifest_path.read_text()


def test_recall_cold_pack_downloads_verifies_and_flips_hot(tmp_path):
    manifest, manifest_path, pack = _manifest_with_pack(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    store.put_file("ccc/env-a/packs/env-a.sqfs", pack)
    cold = replace(manifest, s3=S3Info(pack_state="cold", uri="ccc/env-a"))

    recalled = recall_cold_pack(cold, manifest_path, store, tmp_path / "hot-packs")

    assert recalled.s3.pack_state == "hot"
    assert recalled.pack_stack.lowers[0].path.startswith(str(tmp_path / "hot-packs"))
    assert sha256_file(recalled.pack_stack.lowers[0].path) == manifest.pack_stack.lowers[0].sha256


def test_recall_cold_pack_rejects_corrupt_object_and_stays_cold(tmp_path):
    manifest, manifest_path, _pack = _manifest_with_pack(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("ccc/env-a/packs/env-a.sqfs", b"corrupt")
    cold = replace(manifest, s3=S3Info(pack_state="cold", uri="ccc/env-a"))

    with pytest.raises(RecallError):
        recall_cold_pack(cold, manifest_path, store, tmp_path / "hot-packs")

    assert not (tmp_path / "hot-packs" / "env-a.sqfs").exists()
    assert 'pack_state = "missing"' in manifest_path.read_text()
