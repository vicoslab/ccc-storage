from __future__ import annotations

import asyncio
import os

import pytest

from ccc_storage_core.observe import OBSERVE_MARKER_NAME
from ccc_storage_mountd import childmount
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_mountd.dispatcher_fuse import ObservationDispatchCore, ObservationFuseOperations
from ccc_storage_mountd.observation import ObservationStorage, initialize_observation_dir


class FakePyFuse3:
    ROOT_INODE = 1

    class FUSEError(Exception):
        def __init__(self, errno):
            super().__init__(errno)
            self.errno = errno

    class EntryAttributes:
        pass

    class FileInfo:
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


def test_dispatch_core_hides_and_reserves_state_dir(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    (source / ".ccc-storage" / "registry").mkdir(parents=True)
    (source / "user1").mkdir()
    core = ObservationDispatchCore(source, mount_root, service)

    assert core.listdir("") == [OBSERVE_MARKER_NAME, "user1"]
    with pytest.raises(ValueError):
        core.mkdir(".ccc-storage")
    with pytest.raises(FileNotFoundError):
        core.entry_for(".ccc-storage")


def test_dispatch_core_passthrough_reads_unmanaged_files(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    (source / "note.txt").write_text("hello from nfs\n")
    core = ObservationDispatchCore(source, mount_root, service)

    entry = core.entry_for("note.txt")

    assert entry.kind == "file"
    assert entry.size == len("hello from nfs\n")
    assert core.read("note.txt", size=5, offset=6) == b"from "


def test_dispatcher_passthrough_create_write_and_unlink_unmanaged_file(fake_nfs, tmp_path):
    source, mount_root, service = _service(fake_nfs, tmp_path)
    core = ObservationDispatchCore(source, mount_root, service)
    ops = ObservationFuseOperations(core, FakePyFuse3)

    async def scenario():
        file_info, attrs = await ops.create(
            FakePyFuse3.ROOT_INODE,
            b"note.txt",
            0o644,
            os.O_WRONLY | os.O_TRUNC,
        )
        assert attrs.st_size == 0
        assert await ops.write(file_info.fh, 0, b"hello") == 5
        assert await ops.write(file_info.fh, 5, b" world") == 6
        await ops.release(file_info.fh)
        assert (source / "note.txt").read_text() == "hello world"

        lookup_attrs = await ops.lookup(FakePyFuse3.ROOT_INODE, b"note.txt")
        file_info = await ops.open(lookup_attrs.st_ino, os.O_RDONLY)
        assert await ops.read(file_info.fh, 6, 5) == b"world"
        await ops.release(file_info.fh)

        await ops.unlink(FakePyFuse3.ROOT_INODE, b"note.txt")

    asyncio.run(scenario())

    assert not (source / "note.txt").exists()


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


def test_dispatch_core_uses_private_source_with_configured_public_root(fake_nfs, tmp_path):
    source = tmp_path / "source"
    public = tmp_path / "public"
    source.mkdir()
    public.mkdir()
    initialized = initialize_observation_dir(source)
    storage = ObservationStorage(
        public_path=public,
        source_root=source,
        state_dir=initialized.state_dir,
        state_subdir=initialized.state_subdir,
        root_id=initialized.root_id,
    )
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observation_dirs=(storage,),
    )
    core = ObservationDispatchCore(source, public, service, materialize_mountpoints=False)

    entry = core.mkdir("env-a")

    assert entry.kind == "dir"
    assert (source / "env-a").is_dir()
    assert not (public / "env-a").exists()
    assert (source / ".ccc-storage" / "registry" / "observe" / "env-a.toml").is_file()


def test_dispatch_core_routes_registered_child_paths_to_private_mount(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(packs), overlay_paths, mountpoint, prefer_kernel, kwargs))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    source = tmp_path / "source"
    public = tmp_path / "public"
    source.mkdir()
    public.mkdir()
    initialized = initialize_observation_dir(source)
    storage = ObservationStorage(
        public_path=public,
        source_root=source,
        state_dir=initialized.state_dir,
        state_subdir=initialized.state_subdir,
        root_id=initialized.root_id,
    )
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observation_dirs=(storage,),
    )
    core = ObservationDispatchCore(source, public, service, materialize_mountpoints=False)
    core.mkdir("env-a")

    private_path = core.source_path("env-a/nested/file.txt")

    assert calls
    assert private_path == calls[0][2] / "nested" / "file.txt"
    assert not str(private_path).startswith(str(source / "env-a"))
    assert service.mounts.active_ids() == [service.handle_observe_ls()["children"][0]["id"]]


def test_dispatcher_registered_child_write_uses_private_mount(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(packs), overlay_paths, mountpoint, prefer_kernel, kwargs))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    source = tmp_path / "source"
    public = tmp_path / "public"
    source.mkdir()
    public.mkdir()
    initialized = initialize_observation_dir(source)
    storage = ObservationStorage(
        public_path=public,
        source_root=source,
        state_dir=initialized.state_dir,
        state_subdir=initialized.state_subdir,
        root_id=initialized.root_id,
    )
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observation_dirs=(storage,),
    )
    core = ObservationDispatchCore(source, public, service, materialize_mountpoints=False)
    ops = ObservationFuseOperations(core, FakePyFuse3)
    core.mkdir("env-a")

    async def scenario():
        env_attrs = await ops.lookup(FakePyFuse3.ROOT_INODE, b"env-a")
        file_info, _attrs = await ops.create(
            env_attrs.st_ino,
            b"created.txt",
            0o644,
            os.O_WRONLY | os.O_TRUNC,
        )
        assert await ops.write(file_info.fh, 0, b"payload") == 7
        await ops.release(file_info.fh)

    asyncio.run(scenario())

    assert calls
    assert (calls[0][2] / "created.txt").read_text() == "payload"
    assert not (source / "env-a" / "created.txt").exists()


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
