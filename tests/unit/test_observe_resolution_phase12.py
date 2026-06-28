from __future__ import annotations

from ccc_layered_core.observe import (
    OBSERVE_MARKER_NAME,
    discover_observation_roots,
    immediate_child_boundaries,
    resolve_observed_child,
)


def test_marker_at_root_makes_top_level_dirs_child_mountpoints(tmp_path):
    (tmp_path / OBSERVE_MARKER_NAME).write_text("")
    (tmp_path / "user1" / "file.txt").parent.mkdir()
    (tmp_path / "user2" / "file.txt").parent.mkdir()
    (tmp_path / "plain.txt").write_text("not a child")

    assert [root.relative_path for root in discover_observation_roots(tmp_path)] == [""]
    assert immediate_child_boundaries(tmp_path) == ("user1", "user2")
    assert resolve_observed_child(tmp_path, "user1/file.txt").boundary_path == "user1"
    assert resolve_observed_child(tmp_path, "user2").boundary_path == "user2"
    assert resolve_observed_child(tmp_path, "plain.txt") is None


def test_marker_under_user_conda_only_observes_immediate_dirs_there(tmp_path):
    observe_root = tmp_path / "user1" / "conda"
    (observe_root / OBSERVE_MARKER_NAME).parent.mkdir(parents=True)
    (observe_root / OBSERVE_MARKER_NAME).write_text("")
    (observe_root / "env-a" / "bin").mkdir(parents=True)
    (tmp_path / "user1" / "project").mkdir()

    assert immediate_child_boundaries(tmp_path) == ("user1/conda/env-a",)
    assert resolve_observed_child(tmp_path, "user1/conda/env-a/bin/python").boundary_path == (
        "user1/conda/env-a"
    )
    assert resolve_observed_child(tmp_path, "user1/project") is None


def test_nearest_observation_root_wins_and_sibling_prefixes_do_not_match(tmp_path):
    (tmp_path / OBSERVE_MARKER_NAME).write_text("")
    nested = tmp_path / "user1" / "conda"
    nested.mkdir(parents=True)
    (nested / OBSERVE_MARKER_NAME).write_text("")
    (nested / "env-a" / "bin").mkdir(parents=True)
    (tmp_path / "user10" / "conda" / "env-b").mkdir(parents=True)

    assert resolve_observed_child(tmp_path, "user1/conda/env-a/bin/python").boundary_path == (
        "user1/conda/env-a"
    )
    assert resolve_observed_child(tmp_path, "user1/project.txt").boundary_path == "user1"
    assert resolve_observed_child(tmp_path, "user10/conda/env-b").boundary_path == "user10"
    assert resolve_observed_child(tmp_path, "user1prefix/thing") is None


def test_removing_marker_means_path_is_no_longer_observed(tmp_path):
    marker = tmp_path / "user1" / "conda" / OBSERVE_MARKER_NAME
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    (marker.parent / "env-a").mkdir()

    assert resolve_observed_child(tmp_path, "user1/conda/env-a") is not None

    marker.unlink()

    assert discover_observation_roots(tmp_path) == ()
    assert immediate_child_boundaries(tmp_path) == ()
    assert resolve_observed_child(tmp_path, "user1/conda/env-a") is None


def test_observed_paths_reject_traversal_absolute_and_empty_components(tmp_path):
    (tmp_path / OBSERVE_MARKER_NAME).write_text("")
    bad_paths = (
        "..",
        "../outside",
        "/abs",
        "user1/../x",
        "user1//x",
        "user1/./x",
        "bad\x00name",
    )

    for rel_path in bad_paths:
        assert resolve_observed_child(tmp_path, rel_path, allow_missing=True) is None
