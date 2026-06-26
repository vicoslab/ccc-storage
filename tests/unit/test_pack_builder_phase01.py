from __future__ import annotations

import subprocess

import pytest

from ccc_layered_pack import builder
from ccc_layered_pack.builder import PackBuildError, build_pack, count_files


def test_count_files_excludes_child_boundaries(tmp_path):
    (tmp_path / "root.txt").write_text("root")
    child = tmp_path / "conda" / "envs" / "env-a"
    child.mkdir(parents=True)
    (child / "python").write_text("py")

    assert count_files(tmp_path) == 2
    assert count_files(tmp_path, exclude_boundaries=["conda/envs/env-a"]) == 1


def test_build_pack_fails_clearly_when_mksquashfs_missing(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(builder.shutil, "which", lambda name: None)

    with pytest.raises(PackBuildError, match="mksquashfs"):
        build_pack(src, tmp_path / "out.sqfs")


def test_build_pack_invokes_mksquashfs_with_deterministic_defaults_and_excludes(
    monkeypatch,
    tmp_path,
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hi")
    out = tmp_path / "out.sqfs"

    monkeypatch.setattr(
        builder.shutil,
        "which",
        lambda name: "/bin/mksquashfs" if name == "mksquashfs" else None,
    )

    def fake_run(args, capture_output, text, check):
        out.write_bytes(b"sqfs-bytes")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = build_pack(src, out, exclude_boundaries=["child-pack"])

    assert result.pack.path == str(out)
    assert result.pack.file_count == 1
    assert "-noappend" in result.args
    assert "-no-progress" in result.args
    assert "-comp" in result.args
    assert "zstd" in result.args
    assert "-e" in result.args
    assert "child-pack" in result.args
