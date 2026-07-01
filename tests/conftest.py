"""Test session wiring: hard isolation guard, markers, and shared fixtures.

Order matters. This module:
  1. puts the repo root + ``src`` on ``sys.path`` (so tests run without an
     install),
  2. validates ``$CCC_TEST_ROOT`` is inside the workspace and exports a sane
     default (the guard) — *before* anything that touches the filesystem,
  3. registers pytest markers and exposes the fakes as fixtures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- 1. make the repo importable without an editable install ----------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

# --- 2. isolation guard (RK-13): abort the whole session if violated ---------
from tests.fakes.isolation import IsolationError, check_test_root  # noqa: E402

try:
    _TEST_ROOT = check_test_root()
except IsolationError as exc:  # pragma: no cover - exercised via subprocess test
    raise SystemExit(f"\nISOLATION GUARD FAILED\n{exc}\n") from exc

os.environ.setdefault("CCC_TEST_ROOT", str(_TEST_ROOT))
_TEST_ROOT.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

from tests.fakes import capability  # noqa: E402
from tests.fakes import fake_nfs as fake_nfs_mod  # noqa: E402
from tests.fakes import fake_s3 as fake_s3_mod  # noqa: E402
from tests.fakes import gen_trees as gen_trees_mod  # noqa: E402

_MARKERS = [
    "fuse: requires unprivileged FUSE (squashfuse / fuse-overlayfs)",
    "kernel_mount: requires kernel mount privilege via user+mount namespaces",
    "userns: requires user namespaces (unshare -r)",
    "multinode: multi-node sync simulation on a shared fake-NFS",
    "bench: performance benchmark (smoke in CI, full manually)",
    "docker: requires a reachable Docker daemon (optional lane)",
    "slow: slow test, excluded from the fast inner loop",
]


def pytest_configure(config: pytest.Config) -> None:
    # Re-validate the guard at configure time; abort cleanly if it ever fails.
    try:
        check_test_root()
    except IsolationError as exc:
        raise pytest.UsageError(str(exc)) from exc
    for marker in _MARKERS:
        config.addinivalue_line("markers", marker)


# --- 3. fixtures -------------------------------------------------------------


@pytest.fixture(scope="session")
def caps() -> capability.Caps:
    """The session-cached capability probe result."""
    return capability.CAPS


@pytest.fixture
def test_root() -> Path:
    return Path(os.environ["CCC_TEST_ROOT"])


@pytest.fixture
def fake_nfs(test_root: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """A fresh fake-NFS tree; sets ``$CCC_NFS_ROOT`` for the test's duration."""
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    monkeypatch.setenv("CCC_NFS_ROOT", str(nfs.ccc_storage))
    try:
        yield nfs
    finally:
        nfs.cleanup()


@pytest.fixture
def fake_s3() -> object:
    """An in-process moto-backed S3; skips cleanly if moto is unavailable."""
    pytest.importorskip("moto", reason="moto not installed; fake-S3 unavailable")
    with fake_s3_mod.fake_s3() as info:
        yield info


@pytest.fixture
def gen_trees() -> object:
    """The synthetic-tree generator module."""
    return gen_trees_mod
