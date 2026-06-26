from __future__ import annotations

from ccc_layered_core.manifest import ChildBoundary, ChildManifest
from ccc_layered_core.resolve import BoundaryRegistry, resolve_owner_path


def _parent() -> ChildManifest:
    return ChildManifest(
        id="user-root:alice",
        name="alice",
        type="user-root",
        generation=12,
        child_boundaries=(
            ChildBoundary("conda/envs/env-a", "conda-env:alice:env-a"),
            ChildBoundary("conda/envs/env-b", "conda-env:alice:env-b"),
        ),
    )


def _child_a() -> ChildManifest:
    return ChildManifest(
        id="conda-env:alice:env-a",
        name="env-a",
        type="conda-env",
        generation=5,
        parent_id="user-root:alice",
        parent_path="conda/envs/env-a",
    )


def test_resolve_owner_longest_prefix_for_nested_child():
    owner = resolve_owner_path(
        _parent().child_boundaries,
        "/conda/envs/env-a/bin/python",
        parent_id="user-root:alice",
    )
    assert owner.owner_id == "conda-env:alice:env-a"
    assert owner.inner_path == "bin/python"
    assert not owner.is_parent


def test_resolve_owner_parent_owns_paths_outside_boundaries():
    owner = resolve_owner_path(
        _parent().child_boundaries,
        "Projects/notes.txt",
        parent_id="user-root:alice",
    )
    assert owner.is_parent
    assert owner.owner_id == "user-root:alice"
    assert owner.inner_path == "Projects/notes.txt"


def test_resolve_owner_does_not_match_sibling_prefix():
    # conda/envs/env-a2 must NOT be owned by the conda/envs/env-a boundary.
    owner = resolve_owner_path(
        _parent().child_boundaries,
        "conda/envs/env-a2/x",
        parent_id="user-root:alice",
    )
    assert owner.is_parent
    assert owner.owner_id == "user-root:alice"


def test_resolve_owner_prefers_deeper_boundary():
    boundaries = (
        ChildBoundary("conda", "c-root"),
        ChildBoundary("conda/envs/env-a", "c-a"),
    )
    owner = resolve_owner_path(boundaries, "conda/envs/env-a/bin/python", parent_id="root")
    assert owner.owner_id == "c-a"
    assert owner.inner_path == "bin/python"


def test_registry_bidirectional_lookup():
    reg = BoundaryRegistry.from_manifests([_parent(), _child_a()])
    assert [b.child_id for b in reg.boundaries_of("user-root:alice")] == [
        "conda-env:alice:env-a",
        "conda-env:alice:env-b",
    ]
    ref = reg.parent_of("conda-env:alice:env-a")
    assert ref is not None
    assert ref.parent_id == "user-root:alice"
    assert ref.parent_path == "conda/envs/env-a"

    owner = reg.resolve_owner("user-root:alice", "conda/envs/env-b/x")
    assert owner.owner_id == "conda-env:alice:env-b"


def test_commit_owner_chooses_nearest_boundary():
    parent = _parent()
    # A change under env-a is owned by the env-a child -> child commit target.
    under_child = resolve_owner_path(
        parent.child_boundaries, "conda/envs/env-a/lib/x.py", parent_id=parent.id
    )
    # A change outside any boundary is owned by the parent -> parent commit.
    outside = resolve_owner_path(parent.child_boundaries, "shell/.bashrc", parent_id=parent.id)
    assert under_child.owner_id == "conda-env:alice:env-a"
    assert outside.owner_id == parent.id
    assert outside.is_parent
