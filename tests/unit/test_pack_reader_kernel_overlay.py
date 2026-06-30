from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ccc_layered_mountd.overlay import LocalOverlayPaths
from ccc_layered_pack import reader


@dataclass
class FakeLowerHandle:
    mountpoint: Path
    command: tuple[str, ...] = ("fake-lower",)
    mounted: bool = True
    unmount_calls: int = 0

    def unmount(self) -> None:
        self.unmount_calls += 1
        self.mounted = False


def test_kernel_overlay_rw_generation0_uses_local_upper_and_work(monkeypatch, tmp_path):
    commands = []

    def fake_which(name):
        return "/bin/mount" if name == "mount" else None

    def fake_run(cmd, capture_output, text, check):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reader.shutil, "which", fake_which)
    monkeypatch.setattr(reader.subprocess, "run", fake_run)
    local = LocalOverlayPaths(
        root=tmp_path / "local" / "child",
        active_upper=tmp_path / "local" / "child" / "active",
        work=tmp_path / "local" / "child" / "work",
        meta=tmp_path / "local" / "child" / "meta.json",
    )

    handle = reader.mount_layered_rw_kernel_overlay((), local, tmp_path / "mnt")

    assert handle.mounted is True
    assert commands[0][:4] == ["/bin/mount", "-t", "overlay", "overlay"]
    opts = commands[0][5]
    assert "lowerdir=" in opts
    assert f"upperdir={local.active_upper}" in opts
    assert f"workdir={local.work}" in opts
    assert local.active_upper.is_dir()
    assert local.work.is_dir()
