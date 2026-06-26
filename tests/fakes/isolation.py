"""Hard test-isolation guard (planning README §7, phase-00 task 4, RK-13).

No test may read or write real ``/storage`` datasets/users or the real
``/storage/.ccc-layered``. Every runtime artifact must live under
``$CCC_TEST_ROOT``, which **must** resolve inside this workspace. These are pure
functions so they can be unit-tested directly; ``tests/conftest.py`` calls
:func:`check_test_root` at session start and aborts the whole session on
failure.
"""

from __future__ import annotations

import os
from pathlib import Path


class IsolationError(RuntimeError):
    """Raised when $CCC_TEST_ROOT would escape the allowed workspace."""


def repo_root() -> Path:
    """Repository root (the ``ccc-layered-storage`` dir).

    This file is at ``<repo>/tests/fakes/isolation.py``.
    """
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    """The outer workspace that all test artifacts must stay within.

    This is the parent of the repo, i.e.
    ``/storage/user/agent-workspace/conda-compute-cluster/ccc-squashfs-storage``
    in this deployment. Derived from the repo location so the guard keeps
    working if the tree is relocated.
    """
    return repo_root().parent


def default_test_root() -> Path:
    """Default ``$CCC_TEST_ROOT`` — the git-ignored ``.scratch`` under the repo."""
    return repo_root() / ".scratch"


def resolve_test_root(env: dict[str, str] | None = None) -> Path:
    """Resolve the configured test root from the environment (or the default)."""
    environ = os.environ if env is None else env
    value = environ.get("CCC_TEST_ROOT")
    if value:
        return Path(value).expanduser().resolve()
    return default_test_root().resolve()


def is_inside(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True iff *path* is *parent* or lives underneath it (after resolution)."""
    rpath = Path(path).resolve()
    rparent = Path(parent).resolve()
    try:
        return os.path.commonpath([str(rpath), str(rparent)]) == str(rparent)
    except ValueError:
        # Different anchors (e.g. relative vs absolute, or different drives).
        return False


def check_test_root(
    test_root: str | os.PathLike[str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate the test root, returning it resolved; raise :class:`IsolationError`.

    Passing *test_root*/​*workspace* explicitly is for unit tests; production
    callers pass nothing and the env/default + derived workspace are used.
    """
    ws = Path(workspace).resolve() if workspace is not None else workspace_root()
    tr = Path(test_root).expanduser().resolve() if test_root is not None else resolve_test_root()
    if not is_inside(tr, ws):
        raise IsolationError(
            f"$CCC_TEST_ROOT must resolve inside the workspace.\n"
            f"  test_root: {tr}\n"
            f"  workspace: {ws}\n"
            f"Refusing to run: tests must never touch real /storage data (RK-13)."
        )
    return tr
