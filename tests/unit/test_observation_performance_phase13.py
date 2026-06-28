from __future__ import annotations

import time

from ccc_layered_core.observe import OBSERVE_MARKER_NAME
from ccc_layered_mountd import childmount
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.dispatcher_fuse import ObservationDispatchCore


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def test_observation_mkdir_and_readdir_are_instant_and_do_not_mount(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    mount_calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        mount_calls.append((tuple(packs), overlay_paths, mountpoint, kwargs))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
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
    core = ObservationDispatchCore(source, mount_root, service)

    start = time.perf_counter()
    for idx in range(120):
        core.mkdir(f"child-{idx:03d}")
    mkdir_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    names = core.listdir("")
    list_elapsed = time.perf_counter() - start

    assert "child-119" in names
    assert len([name for name in names if name.startswith("child-")]) == 120
    assert mount_calls == []
    assert service.mounts.active_count() == 0
    # This is intentionally generous for shared/loaded CI, but catches recursive
    # pack builds, mounts, or tree walks accidentally added to mkdir/readdir.
    assert mkdir_elapsed < 5.0
    assert list_elapsed < 1.0

    mounted = core.ensure_mounted_for("child-023/file.txt")

    assert mounted is not None
    assert mounted["id"] == "observe:child-023"
    assert len(mount_calls) == 1
    assert service.mounts.active_ids() == ["observe:child-023"]
