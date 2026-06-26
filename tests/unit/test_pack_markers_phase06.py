from __future__ import annotations

from ccc_layered_pack.builder import (
    BOUNDARY_MARKER_NAME,
    count_files,
    create_boundary_markers,
    plan_boundary_markers,
)


def test_plan_boundary_markers_lists_dirs_and_markers():
    plan = plan_boundary_markers(["conda/envs/env-a", "/conda/envs/env-b/"])
    assert plan.boundary_paths == ("conda/envs/env-a", "conda/envs/env-b")
    assert plan.marker_files == (
        f"conda/envs/env-a/{BOUNDARY_MARKER_NAME}",
        f"conda/envs/env-b/{BOUNDARY_MARKER_NAME}",
    )


def test_create_boundary_markers_creates_dirs_without_child_payload(tmp_path):
    src = tmp_path / "root"
    src.mkdir()
    (src / "hello.txt").write_text("hi")

    created = create_boundary_markers(src, ["conda/envs/env-a"])

    marker = src / "conda" / "envs" / "env-a" / BOUNDARY_MARKER_NAME
    assert marker.exists()
    assert marker in created
    # The boundary dir is navigable but holds only the marker, no child payload.
    assert sorted(p.name for p in (src / "conda" / "envs" / "env-a").iterdir()) == [
        BOUNDARY_MARKER_NAME
    ]


def test_exclusion_drops_child_payload_but_keeps_parent(tmp_path):
    src = tmp_path / "root"
    (src / "conda" / "envs" / "env-a").mkdir(parents=True)
    (src / "hello.txt").write_text("hi")
    (src / "conda" / "envs" / "env-a" / "big.bin").write_text("payload")

    assert count_files(src) == 2
    assert count_files(src, exclude_boundaries=["conda/envs/env-a"]) == 1
