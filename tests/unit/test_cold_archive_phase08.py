from __future__ import annotations

import pytest

from ccc_storage_cold.archive import (
    RecallError,
    archive_committed_packs_to_cold_storage,
    recall_cold_pack,
)
from ccc_storage_cold.object_store import LocalObjectStore, ObjectStoreError
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)


def _manifest_with_base_and_delta(tmp_path):
    pack_dir = tmp_path / "nfs" / "packs" / "dataset-photos"
    pack_dir.mkdir(parents=True)
    base = pack_dir / "base-g0001.sqfs"
    delta = pack_dir / "delta-g0002.sqfs"
    base.write_bytes(b"base-pack-from-folder")
    delta.write_bytes(b"delta-pack-with-new-images")
    manifest = ChildManifest(
        id="dataset:photos",
        name="photos",
        type="dataset",
        generation=2,
        pack_stack=PackStack(
            active_revision="g0002",
            lowers=(
                PackInfo(path=str(base), sha256=sha256_file(base), size=base.stat().st_size),
                PackInfo(path=str(delta), sha256=sha256_file(delta), size=delta.stat().st_size),
            ),
        ),
    )
    manifest_path = tmp_path / "nfs" / "registry" / "photos.toml"
    dump_atomic(manifest_path, manifest)
    return manifest, manifest_path, base, delta


def test_archive_committed_packs_uploads_base_and_delta_marks_cold_and_removes_hot(tmp_path):
    manifest, manifest_path, base, delta = _manifest_with_base_and_delta(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")

    result = archive_committed_packs_to_cold_storage(
        manifest,
        manifest_path,
        store,
        prefix="ccc/datasets/photos/g0002",
        remove_hot=True,
    )

    assert result.manifest.s3.pack_state == "cold"
    assert result.manifest.s3.snapshot_state == "available"
    assert result.manifest.s3.uri == "ccc/datasets/photos/g0002"
    assert result.removed_hot_paths == (str(base), str(delta))
    assert not base.exists()
    assert not delta.exists()
    base_key = "ccc/datasets/photos/g0002/packs/base-g0001.sqfs"
    delta_key = "ccc/datasets/photos/g0002/packs/delta-g0002.sqfs"
    manifest_key = "ccc/datasets/photos/g0002/manifest.toml"
    assert store.read_bytes(base_key) == b"base-pack-from-folder"
    assert store.read_bytes(delta_key) == b"delta-pack-with-new-images"
    assert b'pack_state = "cold"' in store.read_bytes(manifest_key)
    assert load_manifest(manifest_path).s3.pack_state == "cold"

    recalled = recall_cold_pack(
        load_manifest(manifest_path),
        manifest_path,
        store,
        tmp_path / "hot-packs",
    )

    assert recalled.s3.pack_state == "hot"
    assert [sha256_file(pack.path) for pack in recalled.pack_stack.lowers] == [
        manifest.pack_stack.lowers[0].sha256,
        manifest.pack_stack.lowers[1].sha256,
    ]


def test_archive_failure_keeps_authoritative_manifest_and_hot_packs(tmp_path):
    manifest, manifest_path, base, delta = _manifest_with_base_and_delta(tmp_path)

    class FailingStore(LocalObjectStore):
        def put_file(self, key, source):  # type: ignore[no-untyped-def]
            raise ObjectStoreError("upload failed")

    with pytest.raises(ObjectStoreError):
        archive_committed_packs_to_cold_storage(
            manifest,
            manifest_path,
            FailingStore(tmp_path / "objects"),
            prefix="ccc/fail",
            remove_hot=True,
        )

    assert base.exists()
    assert delta.exists()
    assert load_manifest(manifest_path).s3.pack_state == "missing"


def test_recall_cold_pack_leaves_manifest_cold_on_corrupt_s3_object(tmp_path):
    manifest, manifest_path, _base, _delta = _manifest_with_base_and_delta(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    cold = archive_committed_packs_to_cold_storage(
        manifest,
        manifest_path,
        store,
        prefix="ccc/corrupt",
        remove_hot=True,
    ).manifest
    store.put_bytes("ccc/corrupt/packs/delta-g0002.sqfs", b"corrupt")

    with pytest.raises(RecallError):
        recall_cold_pack(cold, manifest_path, store, tmp_path / "hot-packs")

    assert load_manifest(manifest_path).s3.pack_state == "cold"
    assert not any((tmp_path / "hot-packs").glob("*.sqfs"))
