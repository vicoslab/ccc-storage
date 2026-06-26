from __future__ import annotations

from ccc_layered_mountd.overlay import (
    OverlayPaths,
    dirty_stats,
    ensure_active_upper,
    seal_active_upper,
)


def test_overlay_dirty_stats_and_active_upper_creation(tmp_path):
    paths = OverlayPaths.for_child(tmp_path / "overlays", "dataset:foo")
    active = ensure_active_upper(paths)
    (active / "a.txt").write_text("abc")
    (active / "sub").mkdir()
    (active / "sub" / "b.txt").write_text("de")

    stats = dirty_stats(active)

    assert stats.dirty is True
    assert stats.file_count == 2
    assert stats.bytes == 5


def test_seal_active_upper_rotates_to_new_empty_active(tmp_path):
    paths = OverlayPaths.for_child(tmp_path / "overlays", "dataset:foo")
    active = ensure_active_upper(paths)
    (active / "a.txt").write_text("abc")

    sealed = seal_active_upper(paths, generation=3)

    assert sealed.path.exists()
    assert (sealed.path / "a.txt").read_text() == "abc"
    assert paths.active_upper.exists()
    assert not list(paths.active_upper.iterdir())
