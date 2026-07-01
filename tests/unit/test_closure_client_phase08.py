from __future__ import annotations

import pytest

from ccc_storage_core.manifest import ChildBoundary, ChildManifest
from ccc_storage_hpc.client import ExcludedChildError, StagedPackset
from ccc_storage_hpc.closure import compute_mount_closure


def _manifests():
    home = ChildManifest(
        id="home:alice",
        name="alice-root",
        type="user-root",
        generation=1,
        child_boundaries=(
            ChildBoundary(path="conda/envs/env-a", child_id="env:a"),
            ChildBoundary(path="conda/envs/env-b", child_id="env:b"),
        ),
    )
    env_a = ChildManifest(
        id="env:a",
        name="env-a",
        type="conda-env",
        generation=2,
        parent_id="home:alice",
        parent_path="conda/envs/env-a",
    )
    env_b = ChildManifest(
        id="env:b",
        name="env-b",
        type="conda-env",
        generation=2,
        parent_id="home:alice",
        parent_path="conda/envs/env-b",
    )
    return home, env_a, env_b


def test_mount_closure_includes_selected_child_and_records_excluded_stub():
    home, env_a, env_b = _manifests()

    graph = compute_mount_closure(
        [home, env_a, env_b], root_id="home:alice", include_child_ids={"env:a"}
    )

    assert {node.child_id for node in graph.included} == {"home:alice", "env:a"}
    assert graph.excluded[0].child_id == "env:b"
    assert "not included" in graph.excluded[0].reason


def test_staged_packset_errors_clearly_on_excluded_child_but_allows_included():
    home, env_a, env_b = _manifests()
    graph = compute_mount_closure(
        [home, env_a, env_b], root_id="home:alice", include_child_ids={"env:a"}
    )
    staged = StagedPackset(graph)

    assert staged.lookup("conda/envs/env-a/bin/python").child_id == "env:a"
    with pytest.raises(ExcludedChildError, match="env:b"):
        staged.lookup("conda/envs/env-b/bin/python")
