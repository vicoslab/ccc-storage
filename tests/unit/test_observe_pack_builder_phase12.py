from __future__ import annotations

import subprocess

from ccc_layered_core.observe import OBSERVE_MARKER_NAME, immediate_child_boundaries
from ccc_layered_mountd.overlay import OverlayPaths
from ccc_layered_pack import builder
from ccc_layered_pack.builder import (
    BOUNDARY_MARKER_NAME,
    build_pack,
    pack_object_dir,
    safe_pack_name,
)


def test_build_pack_with_observation_markers_excludes_observed_child_payload(
    monkeypatch,
    tmp_path,
):
    src = tmp_path / "src"
    (src / OBSERVE_MARKER_NAME).parent.mkdir(parents=True)
    (src / OBSERVE_MARKER_NAME).write_text("")
    (src / "parent.txt").write_text("parent")
    (src / "user1" / "payload.txt").parent.mkdir()
    (src / "user1" / "payload.txt").write_text("child payload")
    (src / "user2" / "payload.txt").parent.mkdir()
    (src / "user2" / "payload.txt").write_text("child payload")

    out = tmp_path / "out.sqfs"
    monkeypatch.setattr(
        builder.shutil,
        "which",
        lambda name: "/bin/mksquashfs" if name == "mksquashfs" else None,
    )

    def fake_run(args, capture_output, text, check):
        builder.shutil.copytree(args[1], tmp_path / "staged")
        out.write_bytes(b"sqfs")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = build_pack(src, out, exclude_observed=True)

    staged = tmp_path / "staged"
    assert (staged / OBSERVE_MARKER_NAME).exists()
    assert (staged / "parent.txt").read_text() == "parent"
    assert (staged / "user1" / BOUNDARY_MARKER_NAME).exists()
    assert (staged / "user2" / BOUNDARY_MARKER_NAME).exists()
    assert not (staged / "user1" / "payload.txt").exists()
    assert not (staged / "user2" / "payload.txt").exists()
    assert result.pack.file_count == 2


def test_nested_observed_child_data_uses_separate_pack_namespaces(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / OBSERVE_MARKER_NAME).write_text("")
    nested = src / "user1" / "conda"
    nested.mkdir(parents=True)
    (nested / OBSERVE_MARKER_NAME).write_text("")
    (nested / "env-a" / "bin").mkdir(parents=True)

    assert immediate_child_boundaries(src) == ("user1", "user1/conda/env-a")

    packs = tmp_path / "packs"
    parent_dir = pack_object_dir(packs, "observe:")
    user_dir = pack_object_dir(packs, "observe:user1")
    env_dir = pack_object_dir(packs, "observe:user1/conda/env-a")

    assert user_dir != parent_dir
    assert env_dir != user_dir
    assert user_dir.parent == packs
    assert env_dir.parent == packs


def test_pack_namespace_names_are_collision_free_for_path_like_ids(tmp_path):
    first = "observe:a/b"
    second = "observe:a_b"

    assert safe_pack_name(first) != safe_pack_name(second)
    assert pack_object_dir(tmp_path, first) != pack_object_dir(tmp_path, second)
    assert OverlayPaths.for_child(tmp_path / "overlays", first).root != OverlayPaths.for_child(
        tmp_path / "overlays", second
    ).root
