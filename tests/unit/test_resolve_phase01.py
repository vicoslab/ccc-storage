from __future__ import annotations

from ccc_storage_core.manifest import ChildBoundary, ChildManifest
from ccc_storage_core.resolve import nearest_boundary


def test_nearest_boundary_returns_longest_matching_child_boundary():
    manifest = ChildManifest(
        id="root",
        name="root",
        type="user-root",
        generation=1,
        child_boundaries=(
            ChildBoundary("conda", "conda-root"),
            ChildBoundary("conda/envs/env-a", "conda-env:env-a"),
        ),
    )

    match = nearest_boundary(manifest, "/conda/envs/env-a/bin/python")

    assert match.boundary is not None
    assert match.boundary.child_id == "conda-env:env-a"


def test_nearest_boundary_returns_none_when_path_is_not_nested_child():
    manifest = ChildManifest(id="root", name="root", type="user-root", generation=1)

    assert nearest_boundary(manifest, "Projects/project-a").boundary is None
