from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.manifest import PackInfo
from ccc_layered_mountd.overlay import OverlayPaths
from ccc_layered_pack import reader


def _which(name: str) -> str | None:
    return f"/bin/{name}" if name == "fuse-overlayfs" else None


@dataclass
class FakeLowerHandle:
    mountpoint: Path
    command: tuple[str, ...] = ("fake-lower",)
    mounted: bool = True
    unmount_calls: int = 0

    def unmount(self) -> None:
        self.unmount_calls += 1
        self.mounted = False


def test_mount_layered_rw_generation0_uses_empty_lower_and_shared_upper(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    monkeypatch.setattr(reader.shutil, "which", _which)

    def fake_run(cmd, capture_output, text, check):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reader.subprocess, "run", fake_run)
    overlay = OverlayPaths.for_child(tmp_path / "overlays", "observe:user1")
    mountpoint = tmp_path / "mnt" / "user1"

    handle = reader.mount_layered_rw((), overlay, mountpoint)

    assert handle.mounted is True
    assert overlay.active_upper.is_dir()
    assert (overlay.root / "work").is_dir()
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0] == "/bin/fuse-overlayfs"
    opts = cmd[2]
    assert "lowerdir=" in opts
    assert f"upperdir={overlay.active_upper}" in opts
    assert f"workdir={overlay.root / 'work'}" in opts
    assert str(mountpoint) == cmd[-1]
    assert handle.lower_handles == ()


def test_mount_layered_rw_pack_backed_uses_stack_lower_plus_upper(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    lower = FakeLowerHandle(tmp_path / "lower-mounted")
    lower.mountpoint.mkdir(parents=True)

    monkeypatch.setattr(reader.shutil, "which", _which)
    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        del packs, mountpoint, prefer_kernel
        return lower

    monkeypatch.setattr(reader, "mount_stack_ro", fake_mount_stack_ro)

    def fake_run(cmd, capture_output, text, check):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reader.subprocess, "run", fake_run)
    pack = PackInfo(path=str(tmp_path / "base.sqfs"), sha256="a" * 64, size=4)
    overlay = OverlayPaths.for_child(tmp_path / "overlays", "observe:user1")
    mountpoint = tmp_path / "mnt" / "user1"

    handle = reader.mount_layered_rw((pack,), overlay, mountpoint)

    assert commands
    opts = commands[0][2]
    assert f"lowerdir={lower.mountpoint}" in opts
    assert f"upperdir={overlay.active_upper}" in opts
    assert f"workdir={overlay.root / 'work'}" in opts
    assert handle.lower_handles == (lower,)
    handle.unmount()
    assert lower.unmount_calls == 1
