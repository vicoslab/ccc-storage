from __future__ import annotations

from ccc_storage_mountd.overlay import (
    dirty_mirror_paths,
    latest_dirty_mirror,
    publish_logical_mirror,
)


def test_publish_logical_mirror_creates_complete_epoch_and_latest_pointer(tmp_path):
    source = tmp_path / "merged"
    (source / "class-a").mkdir(parents=True)
    (source / "class-a" / "img001.jpg").write_bytes(b"jpeg")

    mirror = dirty_mirror_paths(tmp_path / "nfs", "observe:dataset")
    first = publish_logical_mirror(
        source,
        mirror,
        child_id="observe:dataset",
        node_id="node-a",
        base_generation=0,
    )

    assert first.epoch == 1
    assert first.file_count == 1
    assert mirror.current.is_symlink()
    assert (mirror.current / "class-a" / "img001.jpg").read_bytes() == b"jpeg"
    latest = latest_dirty_mirror(tmp_path / "nfs", "observe:dataset")
    assert latest is not None
    assert latest.epoch == 1

    (source / "class-a" / "img001.jpg").write_bytes(b"new")
    (source / "class-b").mkdir()
    (source / "class-b" / "img002.jpg").write_bytes(b"new2")
    second = publish_logical_mirror(
        source,
        mirror,
        child_id="observe:dataset",
        node_id="node-a",
        base_generation=0,
    )

    assert second.epoch == 2
    assert (mirror.current / "class-a" / "img001.jpg").read_bytes() == b"new"
    assert (mirror.current / "class-b" / "img002.jpg").read_bytes() == b"new2"
