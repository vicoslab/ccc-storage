from __future__ import annotations

import pytest

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.locks import NFSLock
from ccc_layered_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    ChildManifest,
    OverlayInfo,
    PackInfo,
    dump_atomic,
    load_manifest,
)
from ccc_layered_mountd import childmount, daemon
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.overlay import (
    OverlayPaths,
    dirty_mirror_paths,
    local_overlay_paths,
    publish_logical_mirror,
)
from ccc_layered_pack.builder import BuildResult


def _write_local_async_child(fake_nfs):
    child_id = "observe:env"
    overlays = OverlayPaths.for_child(fake_nfs.ccc_layered / "overlays", child_id)
    manifest = ChildManifest(
        id=child_id,
        name="env",
        type="observed-child",
        generation=0,
        write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
        overlay=OverlayInfo(
            mode="shared-overlay",
            active_upper=str(overlays.active_upper),
            overlay_generation=0,
        ),
    )
    manifest_path = fake_nfs.ccc_layered / "registry" / "observe" / "env.toml"
    dump_atomic(manifest_path, manifest)
    service = MountdService(fake_nfs.ccc_layered, fake_nfs.root / "run")
    service.reload_registry()
    return service, manifest, manifest_path


def test_local_async_status_reports_latest_published_mirror(fake_nfs, tmp_path):
    service, manifest, _ = _write_local_async_child(fake_nfs)
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "created.txt").write_text("created")
    publish_logical_mirror(
        merged,
        dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id),
        child_id=manifest.id,
        node_id="node-a",
        base_generation=manifest.generation,
    )

    status = service.handle_status(manifest.id)

    assert status["state"] == "dirty"
    assert status["overlay"]["dirty"] is True
    assert status["overlay"]["file_count"] == 1
    assert status["overlay"]["latest_dirty_epoch"] == 1


def test_local_async_commit_builds_delta_from_published_mirror_and_cleans_async_state(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    service, manifest, manifest_path = _write_local_async_child(fake_nfs)
    local_active = service.mounts.local_overlay_root / "observe%3Aenv" / "active"
    local_active.mkdir(parents=True, exist_ok=True)
    (local_active / "stale.txt").write_text("stale")
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "created.txt").write_text("created")
    mirror = publish_logical_mirror(
        merged,
        dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id),
        child_id=manifest.id,
        node_id="node-a",
        base_generation=manifest.generation,
    )
    built = []

    def fake_build_delta(src, base_manifest, out, tombstones=None):
        out.write_bytes(b"delta")
        pack = PackInfo(
            path=str(out),
            sha256=sha256_file(out),
            size=out.stat().st_size,
            file_count=1,
        )
        built.append((src, base_manifest.id, out))
        return BuildResult(pack=pack, args=("fake",))

    monkeypatch.setattr(daemon, "build_delta", fake_build_delta)

    result = service.handle_commit(manifest.id, message="local")

    assert built[0][0] == mirror.path
    assert result["generation"] == 1
    assert result["state"] == "clean"
    assert not dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id).root.exists()
    assert not (service.mounts.local_overlay_root / "observe%3Aenv").exists()
    persisted = load_manifest(manifest_path)
    assert persisted.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert persisted.generation == 1


def test_local_async_status_reports_unpublished_local_upper(fake_nfs):
    service, manifest, _ = _write_local_async_child(fake_nfs)
    local = local_overlay_paths(service.mounts.local_overlay_root, manifest.id)
    local.active_upper.mkdir(parents=True, exist_ok=True)
    (local.active_upper / "unpublished.txt").write_text("dirty")

    status = service.handle_status(manifest.id)

    assert status["state"] == "dirty"
    assert status["overlay"]["unpublished_local_dirty"] is True
    assert status["overlay"]["file_count"] == 1


def test_local_async_commit_noops_for_empty_published_mirror(monkeypatch, fake_nfs, tmp_path):
    service, manifest, manifest_path = _write_local_async_child(fake_nfs)
    empty = tmp_path / "empty"
    empty.mkdir()
    publish_logical_mirror(
        empty,
        dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id),
        child_id=manifest.id,
        node_id="node-a",
        base_generation=manifest.generation,
    )

    def fail_build_delta(*args, **kwargs):
        raise AssertionError("empty mirror must not be committed")

    monkeypatch.setattr(daemon, "build_delta", fail_build_delta)

    result = service.handle_commit(manifest.id, message="empty")

    assert result["generation"] == 0
    assert result["state"] == "clean"
    assert load_manifest(manifest_path).generation == 0


def test_local_async_commit_refuses_external_writer_lock(fake_nfs, tmp_path):
    service, manifest, _ = _write_local_async_child(fake_nfs)
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "created.txt").write_text("created")
    publish_logical_mirror(
        merged,
        dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id),
        child_id=manifest.id,
        node_id="node-a",
        base_generation=manifest.generation,
    )
    lock = NFSLock(service.mounts.writer_lock_path(manifest.id), op="test-writer").acquire()
    try:
        with pytest.raises(childmount.ChildMountError):
            service.handle_commit(manifest.id, message="blocked")
    finally:
        lock.release()


def test_local_async_commit_refuses_stale_base_generation(fake_nfs, tmp_path):
    service, manifest, _ = _write_local_async_child(fake_nfs)
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "created.txt").write_text("created")
    publish_logical_mirror(
        merged,
        dirty_mirror_paths(fake_nfs.ccc_layered, manifest.id),
        child_id=manifest.id,
        node_id="node-a",
        base_generation=manifest.generation + 1,
    )

    with pytest.raises(childmount.ChildMountError):
        service.handle_commit(manifest.id, message="stale")