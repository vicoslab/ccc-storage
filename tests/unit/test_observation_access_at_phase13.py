from __future__ import annotations

from ccc_storage_core.observe import OBSERVE_MARKER_NAME
from ccc_storage_mountd import childmount
from ccc_storage_mountd.daemon import MountdService


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def test_observe_access_at_mounts_child_at_visible_dispatcher_path(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(packs), overlay_paths, mountpoint, prefer_kernel))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    source = tmp_path / "source"
    mount_root = tmp_path / "published"
    source.mkdir()
    mount_root.mkdir()
    (mount_root / "user1").mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    service.handle_observe_mkdir("user1")

    mounted = service.handle_observe_access_at("user1/file.txt", str(mount_root / "user1"))

    assert mounted["id"] == "observe:user1"
    assert mounted["mountpoint"] == str(mount_root / "user1")
    assert len(calls) == 1
    assert calls[0][2] == mount_root / "user1"

def test_observe_access_at_reuse_is_idempotent_for_automount_refcount(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    source = tmp_path / "source"
    mount_root = tmp_path / "published"
    source.mkdir()
    mount_root.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    service.handle_observe_mkdir("user1")

    first = service.handle_observe_access_at("user1/file-a", str(mount_root / "user1"))
    second = service.handle_observe_access_at("user1/file-b", str(mount_root / "user1"))

    assert first["refcount"] == 1
    assert second["refcount"] == 1
    assert service.handle_umount("observe:user1")["mounted"] is False

