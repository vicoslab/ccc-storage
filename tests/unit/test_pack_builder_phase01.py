from __future__ import annotations

import subprocess

import pytest

from ccc_layered_pack import builder
from ccc_layered_pack.builder import (
    BOUNDARY_MARKER_NAME,
    PackBuildError,
    build_pack,
    count_files,
    is_overlayfs_artifact,
    prepare_delta_source,
)


def test_count_files_excludes_child_boundaries(tmp_path):
    (tmp_path / "root.txt").write_text("root")
    child = tmp_path / "conda" / "envs" / "env-a"
    child.mkdir(parents=True)
    (child / "python").write_text("py")

    assert count_files(tmp_path) == 2
    assert count_files(tmp_path, exclude_boundaries=["conda/envs/env-a"]) == 1


def test_prepare_delta_source_filters_fuse_overlayfs_whiteout_artifacts(tmp_path):
    src = tmp_path / "upper"
    src.mkdir()
    (src / "plain.txt").write_text("plain")
    writes = src / "client-writes"
    writes.mkdir()
    (writes / "domen-cuda10.txt").write_text("client write")
    (writes / ".wh..wh..opq").write_text("")
    (writes / ".wh.deleted.txt").write_text("")
    (src / ".wh..opq").write_text("")

    dst = tmp_path / "prepared"
    copied = prepare_delta_source(src, dst)

    assert copied == 2
    assert (dst / "plain.txt").read_text() == "plain"
    assert (dst / "client-writes" / "domen-cuda10.txt").read_text() == "client write"
    assert not any(is_overlayfs_artifact(path.name) for path in dst.rglob("*"))
    assert not (dst / "client-writes" / ".wh.deleted.txt").exists()


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
    assert "-e" not in result.args


def test_build_pack_keeps_boundary_stub_but_excludes_child_payload(monkeypatch, tmp_path):
    src = tmp_path / "src"
    boundary = src / "conda" / "envs" / "env-a"
    boundary.mkdir(parents=True)
    (src / "parent-only.txt").write_text("parent")
    (boundary / "bin").mkdir()
    (boundary / "bin" / "python").write_text("child payload must not leak")
    out = tmp_path / "out.sqfs"

    monkeypatch.setattr(
        builder.shutil,
        "which",
        lambda name: "/bin/mksquashfs" if name == "mksquashfs" else None,
    )

    def fake_run(args, capture_output, text, check):
        staged = tmp_path / "observed-stage"
        builder.shutil.copytree(args[1], staged)
        out.write_bytes(b"sqfs-bytes")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = build_pack(src, out, exclude_boundaries=["conda/envs/env-a"])

    staged = tmp_path / "observed-stage"
    assert (staged / "parent-only.txt").read_text() == "parent"
    assert (staged / "conda" / "envs" / "env-a" / BOUNDARY_MARKER_NAME).exists()
    assert not (staged / "conda" / "envs" / "env-a" / "bin" / "python").exists()
    assert result.pack.file_count == 1
