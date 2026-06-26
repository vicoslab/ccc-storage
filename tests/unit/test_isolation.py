"""Isolation guard (RK-13) — the single most safety-critical phase-00 unit.

Tested by calling ``tests.fakes.isolation`` directly (no subprocess): the guard
must reject any ``$CCC_TEST_ROOT`` that resolves outside the workspace and
accept one inside it.
"""

from __future__ import annotations

import pytest

from tests.fakes import isolation


def test_workspace_is_parent_of_repo() -> None:
    assert isolation.workspace_root() == isolation.repo_root().parent


def test_default_test_root_is_scratch_inside_repo() -> None:
    dtr = isolation.default_test_root()
    assert dtr.name == ".scratch"
    assert isolation.is_inside(dtr, isolation.repo_root())


def test_is_inside_basic(tmp_path) -> None:
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert isolation.is_inside(child, tmp_path)
    assert isolation.is_inside(tmp_path, tmp_path)  # parent == path
    assert not isolation.is_inside(tmp_path, child)  # parent below child
    assert not isolation.is_inside("/tmp", "/storage")


@pytest.mark.parametrize("outside", ["/tmp", "/storage/dataset", "/storage/user", "/"])
def test_guard_rejects_outside_workspace(outside: str) -> None:
    with pytest.raises(isolation.IsolationError):
        isolation.check_test_root(test_root=outside)


def test_guard_accepts_inside_workspace() -> None:
    inside = isolation.repo_root() / ".scratch" / "nested"
    resolved = isolation.check_test_root(test_root=inside)
    assert isolation.is_inside(resolved, isolation.workspace_root())


def test_guard_accepts_workspace_root_itself() -> None:
    ws = isolation.workspace_root()
    assert isolation.check_test_root(test_root=ws, workspace=ws) == ws.resolve()


def test_guard_uses_explicit_workspace(tmp_path) -> None:
    inside = tmp_path / "scratch"
    assert isolation.check_test_root(test_root=inside, workspace=tmp_path) == inside.resolve()
    with pytest.raises(isolation.IsolationError):
        isolation.check_test_root(test_root=tmp_path.parent, workspace=tmp_path)


def test_resolve_test_root_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCC_TEST_ROOT", str(tmp_path))
    assert isolation.resolve_test_root() == tmp_path.resolve()


def test_resolve_test_root_default_when_unset() -> None:
    # An explicit empty/absent env dict falls back to the .scratch default.
    assert isolation.resolve_test_root(env={}) == isolation.default_test_root().resolve()
