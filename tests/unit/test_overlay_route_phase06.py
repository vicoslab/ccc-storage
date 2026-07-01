from __future__ import annotations

from ccc_storage_core.manifest import ChildBoundary, ChildManifest
from ccc_storage_mountd.overlay import route_path
from ccc_storage_pack.builder import safe_pack_name


def _parent() -> ChildManifest:
    return ChildManifest(
        id="root",
        name="root",
        type="user-root",
        generation=1,
        child_boundaries=(
            ChildBoundary("conda/envs/env-a", "env:env-a"),
            ChildBoundary("conda/envs/env-b", "env:env-b"),
        ),
    )


def test_route_under_child_boundary_goes_to_child_overlay(tmp_path):
    route = route_path(_parent(), "conda/envs/env-a/lib/new.py", tmp_path / "overlays")
    assert not route.is_parent
    assert route.owner_id == "env:env-a"
    assert route.inner_path == "lib/new.py"
    # The child overlay root is namespaced by the child id, not the parent.
    assert route.overlay.root.name == safe_pack_name("env:env-a")


def test_route_outside_boundary_goes_to_parent_overlay(tmp_path):
    route = route_path(_parent(), "Projects/notes.txt", tmp_path / "overlays")
    assert route.is_parent
    assert route.owner_id == "root"
    assert route.inner_path == "Projects/notes.txt"
    assert "root" in str(route.overlay.root)
