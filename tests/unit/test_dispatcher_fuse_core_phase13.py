from __future__ import annotations

import pytest

from ccc_storage_core.observe import OBSERVE_MARKER_NAME
from ccc_storage_mountd import childmount
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_mountd.dispatcher_fuse import ObservationDispatchCore, ObservationFuseOperations


class FakePyFuse3:
    ROOT_INODE = 1

    class EntryAttributes:
        pass


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def _service(fake_nfs, tmp_path):
    source = tmp_path / "source"
    mount_root = tmp_path / "mounted"
    source.mkdir()
    mount_root.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    return source, mount_root, service


def test_dispatch_core_readdir_lists_marker_and_children_without_mounting(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    (source / "user1").mkdir()
    (source / "root-note.txt").write_text("parent-owned\n")
    core = ObservationDispatchCore(source, mount_root, service)

    names = core.listdir("")

    assert names == [OBSERVE_MARKER_NAME, "root-note.txt", "user1"]
    assert service.mounts.active_count() == 0


def test_dispatch_core_mkdir_registers_child_but_does_not_mount(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    core = ObservationDispatchCore(source, mount_root, service)

    entry = core.mkdir("user1")

    assert entry.kind == "dir"
    assert (source / "user1").is_dir()
    assert (mount_root / "user1").is_dir()
    assert service.mounts.active_count() == 0
    listed = service.handle_observe_ls()["children"]
    assert listed[0]["id"] == "observe:user1"
    assert listed[0]["registered"] is True


def test_dispatcher_attrs_use_configured_client_uid_gid(fake_nfs, tmp_path):
    source = tmp_path / "source"
    mount_root = tmp_path / "mounted"
    source.mkdir()
    mount_root.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
        storage_uid=2094,
        storage_gid=2094,
    )
    core = ObservationDispatchCore(source, mount_root, service)
    ops = ObservationFuseOperations(core, FakePyFuse3)

    attrs = ops._attrs(core.entry_for(""))

    assert attrs.st_uid == 2094
    assert attrs.st_gid == 2094


def test_dispatch_core_lazy_access_mounts_only_requested_child(monkeypatch, fake_nfs, tmp_path):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(packs), overlay_paths, mountpoint, prefer_kernel))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    _source, mount_root, service = _service(fake_nfs, tmp_path)
    core = ObservationDispatchCore(_source, mount_root, service)
    core.mkdir("user1")
    core.mkdir("user2")

    mounted = core.ensure_mounted_for("user1/file.txt")

    assert mounted is not None
    assert mounted["id"] == "observe:user1"
    assert service.mounts.active_ids() == ["observe:user1"]
    assert calls[0][2] == mount_root / "user1"


def test_dispatch_core_rejects_unsafe_names(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    core = ObservationDispatchCore(source, mount_root, service)

    for bad in ("", ".", "..", "a/b", "bad\0name"):
        with pytest.raises(ValueError):
            core.validate_name(bad)
