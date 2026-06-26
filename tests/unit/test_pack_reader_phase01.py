from __future__ import annotations

import subprocess

import pytest

from ccc_layered_pack import reader
from ccc_layered_pack.reader import PackReadError, extract, mount_ro


def test_mount_ro_fails_clearly_without_squashfuse(monkeypatch, tmp_path):
    monkeypatch.setattr(reader.shutil, "which", lambda name: None)

    with pytest.raises(PackReadError, match="squashfuse"):
        mount_ro(tmp_path / "p.sqfs", tmp_path / "mnt")


def test_extract_invokes_unsquashfs(monkeypatch, tmp_path):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"sqfs")
    dest = tmp_path / "out"
    calls = []
    monkeypatch.setattr(
        reader.shutil,
        "which",
        lambda name: "/bin/unsquashfs" if name == "unsquashfs" else None,
    )

    def fake_run(args, capture_output, text, check):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(reader.subprocess, "run", fake_run)

    extract(pack, dest)

    assert dest.exists()
    assert calls and calls[0][0] == "/bin/unsquashfs"
