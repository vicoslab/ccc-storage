from __future__ import annotations

import subprocess

from ccc_layered_core.manifest import PackInfo
from ccc_layered_pack import reader


class FakeMountHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True
        self.unmount_calls = 0

    def unmount(self):
        self.unmount_calls += 1
        self.mounted = False


def _pack(path, payload: bytes) -> PackInfo:
    path.write_bytes(payload)
    return PackInfo(path=str(path), sha256="a" * 64, size=len(payload))


def test_mount_stack_ro_single_pack_delegates_to_mount_ro(monkeypatch, tmp_path):
    pack = _pack(tmp_path / "base.sqfs", b"base")
    calls = []

    def fake_mount_ro(pack_path, mountpoint, prefer_kernel=False):
        calls.append((pack_path, mountpoint, prefer_kernel))
        return FakeMountHandle(mountpoint)

    monkeypatch.setattr(reader, "mount_ro", fake_mount_ro)

    handle = reader.mount_stack_ro((pack,), tmp_path / "mnt", prefer_kernel=True)

    assert handle.mounted is True
    assert calls == [(pack.path, tmp_path / "mnt", True)]


def test_mount_stack_ro_multiple_packs_uses_fuse_overlayfs_with_delta_first(
    monkeypatch,
    tmp_path,
):
    base = _pack(tmp_path / "base.sqfs", b"base")
    delta = _pack(tmp_path / "delta.sqfs", b"delta")
    mount_calls = []
    run_calls = []

    def fake_mount_ro(pack_path, mountpoint, prefer_kernel=False):
        mount_calls.append((pack_path, mountpoint, prefer_kernel))
        return FakeMountHandle(mountpoint)

    def fake_which(name):
        if name == "fuse-overlayfs":
            return "/usr/bin/fuse-overlayfs"
        if name == "fusermount3":
            return "/usr/bin/fusermount3"
        return None

    def fake_run(cmd, capture_output=True, text=True, check=False):
        run_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(reader, "mount_ro", fake_mount_ro)
    monkeypatch.setattr(reader.shutil, "which", fake_which)
    monkeypatch.setattr(reader.subprocess, "run", fake_run)

    handle = reader.mount_stack_ro((base, delta), tmp_path / "mnt")

    assert handle.mounted is True
    assert [call[0] for call in mount_calls] == [base.path, delta.path]
    overlay_cmd = run_calls[0]
    assert overlay_cmd[0] == "/usr/bin/fuse-overlayfs"
    lowerdir_opt = next(item for item in overlay_cmd if item.startswith("lowerdir="))
    base_lower = str(mount_calls[0][1])
    delta_lower = str(mount_calls[1][1])
    assert lowerdir_opt == f"lowerdir={delta_lower}:{base_lower}"
