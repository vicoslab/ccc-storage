from __future__ import annotations

import pytest

from ccc_layered_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    OverlayInfo,
    dump_atomic,
    load_manifest,
)
from ccc_layered_mountd import childmount
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.overlay import (
    OverlayPaths,
    dirty_mirror_paths,
    local_overlay_paths,
    publish_logical_mirror,
)


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def _write_child(fake_nfs, child_id="observe:env", write_policy=WRITE_POLICY_SHARED_NFS):
    overlays = OverlayPaths.for_child(fake_nfs.ccc_layered / "overlays", child_id)
    manifest = ChildManifest(
        id=child_id,
        name="env",
        type="observed-child",
        generation=0,
        write_policy=write_policy,
        overlay=OverlayInfo(
            mode="shared-overlay",
            active_upper=str(overlays.active_upper),
            overlay_generation=0,
        ),
    )
    path = fake_nfs.ccc_layered / "registry" / "observe" / "env.toml"
    dump_atomic(path, manifest)
    return path


def test_service_write_policy_reports_and_sets_clean_child(fake_nfs, tmp_path):
    path = _write_child(fake_nfs)
    service = MountdService(fake_nfs.ccc_layered, tmp_path / "run")

    before = service.handle_write_policy("observe:env")
    assert before["write_policy"] == WRITE_POLICY_SHARED_NFS

    after = service.handle_write_policy("observe:env", policy=WRITE_POLICY_LOCAL_SSD_ASYNC)

    assert after["write_policy"] == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert load_manifest(path).write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC


def test_service_write_policy_refuses_dirty_child(fake_nfs, tmp_path):
    _write_child(fake_nfs)
    service = MountdService(fake_nfs.ccc_layered, tmp_path / "run")
    active = fake_nfs.ccc_layered / "overlays" / "observe%3Aenv" / "active"
    active.mkdir(parents=True)
    (active / "dirty.txt").write_text("dirty")

    with pytest.raises(childmount.ChildMountError):
        service.handle_write_policy("observe:env", policy=WRITE_POLICY_LOCAL_SSD_ASYNC)


def test_service_write_policy_remounts_mounted_child_when_requested(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    path = _write_child(fake_nfs)
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((overlay_paths, mountpoint, prefer_kernel, kwargs))
        return FakeHandle(mountpoint)

    def fake_mount_layered_rw_kernel_overlay(packs, local_paths, mountpoint, **kwargs):
        calls.append((local_paths, mountpoint, True, kwargs))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    monkeypatch.setattr(
        childmount,
        "mount_layered_rw_kernel_overlay",
        fake_mount_layered_rw_kernel_overlay,
    )
    service = MountdService(fake_nfs.ccc_layered, tmp_path / "run")
    service.mounts.mount_rw(service._find("observe:env"))
    mountpoint = service.mounts.status(service._find("observe:env"))["mountpoint"]

    with pytest.raises(childmount.ChildMountError):
        service.handle_write_policy("observe:env", policy=WRITE_POLICY_LOCAL_SSD_ASYNC)

    after = service.handle_write_policy(
        "observe:env",
        policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
        remount=True,
    )

    assert after["write_policy"] == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert after["mounted"] is True
    assert after["mountpoint"] == mountpoint
    assert len(calls) == 2
    assert load_manifest(path).write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC


def test_service_write_policy_refuses_dirty_local_ssd_upper(fake_nfs, tmp_path):
    _write_child(fake_nfs, write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC)
    service = MountdService(fake_nfs.ccc_layered, tmp_path / "run")
    local = local_overlay_paths(service.mounts.local_overlay_root, "observe:env")
    local.active_upper.mkdir(parents=True, exist_ok=True)
    (local.active_upper / "dirty.txt").write_text("dirty")

    with pytest.raises(childmount.ChildMountError):
        service.handle_write_policy("observe:env", policy=WRITE_POLICY_SHARED_NFS)


def test_service_write_policy_refuses_dirty_published_local_mirror(fake_nfs, tmp_path):
    _write_child(fake_nfs, write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC)
    service = MountdService(fake_nfs.ccc_layered, tmp_path / "run")
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "dirty.txt").write_text("dirty")
    publish_logical_mirror(
        merged,
        dirty_mirror_paths(fake_nfs.ccc_layered, "observe:env"),
        child_id="observe:env",
        node_id="node-a",
        base_generation=0,
    )

    with pytest.raises(childmount.ChildMountError):
        service.handle_write_policy("observe:env", policy=WRITE_POLICY_SHARED_NFS)