from __future__ import annotations

import asyncio
import errno

import pytest

from ccc_layered_core.manifest import load_manifest
from ccc_layered_core.observe import OBSERVE_MARKER_NAME
from ccc_layered_mountd import childmount
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.dispatcher_fuse import ObservationDispatchCore, ObservationFuseOperations


class FakeFuseError(Exception):
    def __init__(self, errno_value: int):
        super().__init__(errno_value)
        self.errno = errno_value


class FakeEntryAttributes:
    pass


class FakePyFuse3:
    ROOT_INODE = 1
    FUSEError = FakeFuseError
    EntryAttributes = FakeEntryAttributes


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def _core(fake_nfs, tmp_path):
    source = tmp_path / "source"
    mount_root = tmp_path / "mounted"
    source.mkdir()
    mount_root.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    return source, mount_root, service, ObservationDispatchCore(source, mount_root, service)


def test_dispatch_core_rmdir_removes_clean_generation0_child(fake_nfs, tmp_path):
    source, mount_root, service, core = _core(fake_nfs, tmp_path)
    core.mkdir("user1")
    manifest_path = fake_nfs.ccc_layered / "registry" / "observe" / "user1.toml"
    assert manifest_path.exists()

    removed = core.rmdir("user1")

    assert removed["removed"] == "user1"
    assert not (source / "user1").exists()
    assert not (mount_root / "user1").exists()
    assert not manifest_path.exists()
    assert service.mounts.active_count() == 0


def test_dispatch_core_rename_moves_clean_generation0_child_manifest_and_paths(fake_nfs, tmp_path):
    source, mount_root, _service, core = _core(fake_nfs, tmp_path)
    core.mkdir("user1")

    renamed = core.rename("user1", "user-renamed")

    assert renamed["id"] == "observe:user-renamed"
    assert renamed["parent_path"] == "user-renamed"
    assert not (source / "user1").exists()
    assert (source / "user-renamed").is_dir()
    assert not (mount_root / "user1").exists()
    assert (mount_root / "user-renamed").is_dir()
    assert not (fake_nfs.ccc_layered / "registry" / "observe" / "user1.toml").exists()
    manifest = load_manifest(
        fake_nfs.ccc_layered / "registry" / "observe" / "user-renamed.toml"
    )
    assert manifest.id == "observe:user-renamed"


def test_dispatch_core_rmdir_refuses_mounted_child(monkeypatch, fake_nfs, tmp_path):
    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    _source, _mount_root, _service, core = _core(fake_nfs, tmp_path)
    core.mkdir("user1")
    core.ensure_mounted_for("user1/file.txt")

    with pytest.raises(RuntimeError, match="mounted"):
        core.rmdir("user1")


def test_dispatch_core_rename_refuses_dirty_child(fake_nfs, tmp_path):
    source, _mount_root, service, core = _core(fake_nfs, tmp_path)
    core.mkdir("user1")
    manifest = load_manifest(fake_nfs.ccc_layered / "registry" / "observe" / "user1.toml")
    active = service.overlay_paths(manifest).active_upper
    active.mkdir(parents=True, exist_ok=True)
    (active / "dirty.txt").write_text("dirty\n")

    with pytest.raises(RuntimeError, match="dirty"):
        core.rename("user1", "user2")

    assert (source / "user1").is_dir()

def test_fuse_ops_rmdir_and_rename_delegate_to_core(fake_nfs, tmp_path):
    source, mount_root, _service, core = _core(fake_nfs, tmp_path)
    ops = ObservationFuseOperations(core, FakePyFuse3)
    core.mkdir("remove-me")
    core.mkdir("rename-me")

    asyncio.run(ops.rmdir(FakePyFuse3.ROOT_INODE, b"remove-me"))
    asyncio.run(ops.rename(FakePyFuse3.ROOT_INODE, b"rename-me", 1, b"renamed", 0))

    assert not (source / "remove-me").exists()
    assert (source / "renamed").is_dir()
    assert not (mount_root / "remove-me").exists()
    assert (mount_root / "renamed").is_dir()


def test_fuse_ops_rename_rejects_nonzero_flags(fake_nfs, tmp_path):
    _source, _mount_root, _service, core = _core(fake_nfs, tmp_path)
    ops = ObservationFuseOperations(core, FakePyFuse3)
    core.mkdir("rename-me")

    with pytest.raises(FakeFuseError) as exc_info:
        asyncio.run(ops.rename(FakePyFuse3.ROOT_INODE, b"rename-me", 1, b"renamed", 1))

    assert exc_info.value.errno == errno.EINVAL

def test_fuse_ops_mkdir_mounts_generation0_child_for_immediate_writes(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    mount_calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        mount_calls.append((tuple(packs), overlay_paths, mountpoint, kwargs))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    _source, _mount_root, service, core = _core(fake_nfs, tmp_path)
    ops = ObservationFuseOperations(core, FakePyFuse3)

    asyncio.run(ops.mkdir(FakePyFuse3.ROOT_INODE, b"new-env", 0o755))

    assert len(mount_calls) == 1
    assert service.mounts.active_ids() == ["observe:new-env"]

