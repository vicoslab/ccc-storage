from __future__ import annotations

from dataclasses import replace

from ccc_storage_cold.archive import archive_committed_packs_to_cold_storage
from ccc_storage_cold.config import ColdStorageConfig
from ccc_storage_cold.object_store import LocalObjectStore
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    S3Info,
    dump_atomic,
    load_manifest,
)
from ccc_storage_core.protocol import Request
from ccc_storage_mountd import childmount
from ccc_storage_mountd.daemon import MountdService


def _write_hot_child(fake_nfs, *, cold_info: S3Info | None = None):
    pack = fake_nfs.subdir("packs") / "foo.sqfs"
    pack.write_bytes(b"pack-bytes")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=3,
        pack_stack=PackStack(
            active_revision="g3",
            lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),),
        ),
        s3=cold_info or S3Info(last_accessed_at="2000-01-01T00:00:00Z"),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    dump_atomic(manifest_path, manifest)
    return manifest, manifest_path, pack


def _service(fake_nfs, store, *, archive_enabled=False, idle_seconds=1, remove_hot=True):
    return MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=fake_nfs.root / "run",
        cold_store=store,
        cold_config=ColdStorageConfig(
            enabled=True,
            archive_enabled=archive_enabled,
            prefix="ccc-test/cold",
            idle_seconds=idle_seconds,
            remove_hot=remove_hot,
        ),
    )


def test_cold_archive_and_recall_are_mountd_operations(fake_nfs):
    manifest, manifest_path, pack = _write_hot_child(fake_nfs)
    store = LocalObjectStore(fake_nfs.root / "objects")
    service = _service(fake_nfs, store)
    service.reload_registry()

    archived = service.handle_cold_archive(manifest.id)

    assert archived["cold_storage"]["pack_state"] == "cold"
    assert archived["cold_storage_action"] == "archive"
    assert not pack.exists()
    assert load_manifest(manifest_path).cold_storage.pack_state == "cold"

    recalled = service.handle_cold_recall(manifest.id)

    persisted = load_manifest(manifest_path)
    assert recalled["cold_storage_recalled"] is True
    assert persisted.cold_storage.pack_state == "hot"
    assert persisted.pack_stack.lowers[0].path.endswith("foo.sqfs")
    assert sha256_file(persisted.pack_stack.lowers[0].path) == manifest.pack_stack.lowers[0].sha256


def test_mount_recall_happens_before_child_mount(monkeypatch, fake_nfs):
    manifest, manifest_path, pack = _write_hot_child(fake_nfs)
    store = LocalObjectStore(fake_nfs.root / "objects")
    cold = archive_committed_packs_to_cold_storage(
        manifest,
        manifest_path,
        store,
        prefix="ccc-test/cold/children/foo/g0003",
        remove_hot=True,
    ).manifest
    assert not pack.exists()

    mounted_paths = []

    class FakeHandle:
        def __init__(self, mountpoint):
            self.mountpoint = mountpoint
            self.command = ("fake",)
            self.mounted = True

        def unmount(self):
            self.mounted = False

    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        mounted_paths.extend(pack.path for pack in packs)
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    service = _service(fake_nfs, store)
    service.reload_registry()

    status = service.handle_mount(cold.id)

    persisted = load_manifest(manifest_path)
    assert status["mounted"] is True
    assert status["cold_storage"]["pack_state"] == "hot"
    assert mounted_paths == [persisted.pack_stack.lowers[0].path]
    assert all(
        pack.path.startswith(str(fake_nfs.ccc_storage / "packs"))
        for pack in persisted.pack_stack.lowers
    )


def test_background_cold_archive_skips_legacy_no_access_metadata_then_initializes(fake_nfs):
    manifest, manifest_path, pack = _write_hot_child(fake_nfs, cold_info=S3Info())
    store = LocalObjectStore(fake_nfs.root / "objects")
    service = _service(fake_nfs, store, archive_enabled=True, idle_seconds=0)
    service.reload_registry()

    assert service.run_cold_storage_once() == []

    persisted = load_manifest(manifest_path)
    assert persisted.cold_storage.last_accessed_at
    assert persisted.cold_storage.pack_state == "missing"
    assert pack.exists()


def test_background_cold_archive_eviction_for_idle_clean_hot_child(fake_nfs):
    manifest, manifest_path, pack = _write_hot_child(
        fake_nfs,
        cold_info=S3Info(last_accessed_at="2000-01-01T00:00:00Z"),
    )
    store = LocalObjectStore(fake_nfs.root / "objects")
    service = _service(fake_nfs, store, archive_enabled=True, idle_seconds=1)
    service.reload_registry()

    results = service.run_cold_storage_once()

    assert len(results) == 1
    assert results[0]["id"] == manifest.id
    assert load_manifest(manifest_path).cold_storage.pack_state == "cold"
    assert not pack.exists()


def test_background_cold_archive_can_mirror_without_eviction(fake_nfs):
    manifest, manifest_path, pack = _write_hot_child(
        fake_nfs,
        cold_info=S3Info(last_accessed_at="2000-01-01T00:00:00Z"),
    )
    store = LocalObjectStore(fake_nfs.root / "objects")
    service = _service(fake_nfs, store, archive_enabled=True, idle_seconds=1, remove_hot=False)
    service.reload_registry()

    results = service.run_cold_storage_once()

    assert len(results) == 1
    persisted = load_manifest(manifest_path)
    assert persisted.cold_storage.pack_state == "hot"
    assert persisted.cold_storage.mode == "mirror"
    assert pack.exists()


def test_cold_status_dispatch(fake_nfs):
    manifest, _manifest_path, _pack = _write_hot_child(fake_nfs)
    service = _service(fake_nfs, LocalObjectStore(fake_nfs.root / "objects"))
    service.reload_registry()

    resp = service.dispatch(Request(command="cold-status", path=manifest.id, payload={}))

    assert resp.ok
    assert resp.result["cold_storage"]["configured"] is True


def test_mirror_after_commit_persists_hot_cold_storage_state(monkeypatch, fake_nfs):
    manifest, manifest_path, _pack = _write_hot_child(fake_nfs)
    store = LocalObjectStore(fake_nfs.root / "objects")
    service = _service(fake_nfs, store)
    service.cold_config = replace(service.cold_config, mirror_after_commit=True)
    service.reload_registry()

    mirrored = service._mirror_after_commit_if_configured(manifest)

    persisted = load_manifest(manifest_path)
    assert mirrored.cold_storage.pack_state == "hot"
    assert persisted.cold_storage.pack_state == "hot"
    assert persisted.cold_storage.uri.startswith("ccc-test/cold/children/")
